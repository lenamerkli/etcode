"""The conversation widgets: markdown view, blocks, tool cards, transcript view, composer (offscreen Qt)."""
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QUrl, Qt  # noqa: E402
from PySide6.QtGui import QDesktopServices, QTextDocument  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

from etcode_desktop.core import transcript as t  # noqa: E402
from etcode_desktop.ui.chat_pane import ChatPane  # noqa: E402
from etcode_desktop.ui.composer import MAX_LINES, Composer  # noqa: E402
from etcode_desktop.ui.markdown_view import AutoHeightBrowser  # noqa: E402
from etcode_desktop.ui.transcript_view import AssistantBlock, ToolCardWidget, TranscriptView, UserBlock  # noqa: E402


def pump(qapp, ms=30):
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.003)
    qapp.processEvents()


def shown(widget, qapp, width=500, height=400):
    widget.resize(width, height)
    widget.show()
    pump(qapp)
    return widget


# --- the markdown view ---------------------------------------------------------------------------------------------

def test_markdown_is_rendered_without_its_markup(qapp):
    view = shown(AutoHeightBrowser(), qapp)
    view.set_markdown("# Title\n\nSome **bold** and `code`.\n\n- one\n- two\n")
    text = view.shown_text()
    assert "Title" in text and "bold" in text and "**" not in text and "`" not in text and "one" in text and "two" in text


def test_raw_html_in_markdown_is_not_interpreted(qapp):
    view = shown(AutoHeightBrowser(), qapp)
    view.set_markdown("before <b>bold</b><script>alert(1)</script> after")
    assert "<b>bold</b>" in view.shown_text() and "<script>" in view.shown_text()  # shown as text, not applied
    assert "<b>" not in view.document().toHtml().split("<body")[1].replace("&lt;b&gt;", "")


def test_the_view_grows_and_shrinks_with_its_text(qapp):
    view = shown(AutoHeightBrowser(), qapp, width=300)
    view.set_plain("one line")
    one = view.height()
    view.set_plain("\n".join(f"line {i}" for i in range(20)))
    twenty = view.height()
    assert twenty > one * 5
    view.set_plain("short again")
    assert view.height() == one


def test_the_view_wraps_to_its_width(qapp):
    view = shown(AutoHeightBrowser(), qapp, width=200)
    view.set_plain("word " * 60)
    narrow = view.height()
    view.resize(800, 100)
    pump(qapp)
    assert view.height() < narrow


def test_setting_the_same_text_again_does_nothing(qapp):
    view = shown(AutoHeightBrowser(), qapp)
    view.set_markdown("**same**")
    document = view.document()
    revision = document.revision()
    view.set_markdown("**same**")
    assert view.document().revision() == revision


def test_plain_text_is_shown_as_typed(qapp):
    view = shown(AutoHeightBrowser(), qapp)
    view.set_plain("**not bold** <i>x</i>")
    assert view.shown_text() == "**not bold** <i>x</i>"


def test_a_markdown_image_loads_nothing(qapp):
    view = AutoHeightBrowser()
    assert view.loadResource(QTextDocument.ResourceType.ImageResource, QUrl("file:///etc/hostname")) is None
    assert view.loadResource(QTextDocument.ResourceType.ImageResource, QUrl("https://example.com/x.png")) is None
    view.set_markdown("![x](file:///etc/hostname)")  # must not raise or read the file


def test_a_link_opens_only_after_confirmation_and_only_for_safe_schemes(qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()) or True))
    view = AutoHeightBrowser()
    asked = []
    answers = iter([False, True])
    view.confirm_link = lambda url: asked.append(url.toString()) or next(answers)
    view.anchorClicked.emit(QUrl("https://example.com/a"))
    assert asked == ["https://example.com/a"] and opened == []  # declined
    view.anchorClicked.emit(QUrl("https://example.com/b"))
    assert opened == ["https://example.com/b"]
    for unsafe in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x/y", "smb://host/share"):
        view.anchorClicked.emit(QUrl(unsafe))
    assert len(asked) == 2 and opened == ["https://example.com/b"]  # not even asked


# --- user block ---------------------------------------------------------------------------------------------------------

def user_entry(text="hello", state=t.COMMITTED, error=None, images=()):
    content = [{"type": "text", "text": text}] + [{"type": "image", "hash": h} for h in images]
    return t.UserEntry("u1", content, 1 if state == t.COMMITTED else None, state, error)


def test_user_block_states(qapp):
    block = UserBlock()
    cases = {t.COMMITTED: "", t.SENDING: "sending", t.QUEUED: "queued", t.FAILED: "not sent: boom", t.UNKNOWN: "not confirmed: boom"}
    for state, expected in cases.items():
        block.update_from(user_entry("hi", state, "boom"))
        assert expected in block.state_text
        assert block._state.isHidden() == (expected == "")
    assert "may still have arrived" in block.state_text


def test_user_block_shows_text_and_images(qapp):
    block = UserBlock()
    block.update_from(user_entry("look at this", images=["a" * 64, "b" * 64]))
    assert block.body_text() == "look at this"
    assert [t.text() for t in block.thumbnails] == ["[image aaaaaaaa…]", "[image bbbbbbbb…]"]  # until the pictures are loaded
    block.update_from(user_entry("", images=["a" * 64]))
    assert block._body.isHidden() and not block._pictures.isHidden() and len(block.thumbnails) == 1


# --- assistant block and tool cards ---------------------------------------------------------------------------------------------

def assistant_entry(text="", reasoning="", calls=(), status="complete", error=None, index=2):
    return t.AssistantEntry("a1", "g", index, text, reasoning, [t.ToolCallView(*c) for c in calls], status, error)


def test_assistant_block_text_and_reasoning(qapp):
    block = AssistantBlock()
    block.update_from(assistant_entry("The **answer**", reasoning="let me think"))
    assert block.body_text() == "The answer"
    assert block.thinking_visible and block.reasoning_shown_text() == "let me think"
    assert block._reasoning.isHidden()  # collapsed until asked for
    block.set_thinking_expanded(True)
    assert not block._reasoning.isHidden() and block.thinking_text.startswith("▾")
    block.set_thinking_expanded(False)
    assert block._reasoning.isHidden() and block.thinking_text.startswith("▸")


def test_assistant_block_without_reasoning_has_no_toggle(qapp):
    block = AssistantBlock()
    block.update_from(assistant_entry("just text"))
    assert not block.thinking_visible
    block.update_from(assistant_entry("just text", reasoning="   \n"))
    assert not block.thinking_visible


def test_streaming_assistant_block_shows_progress(qapp):
    block = AssistantBlock()
    block.update_from(assistant_entry("", status="streaming", index=None))
    assert block.note_text == "…" and block._body.isHidden()
    block.update_from(assistant_entry("Hel", status="streaming", index=None))
    assert block.note_text == "" and block.body_text() == "Hel"
    block.update_from(assistant_entry("", reasoning="hm", status="streaming", index=None))
    assert block.note_text == "" and block.thinking_text.endswith("Thinking…")


def test_assistant_block_error_and_cancelled_notes(qapp):
    block = AssistantBlock()
    block.update_from(assistant_entry("partial", status="error", error={"code": "upstream_error", "message": "provider fell over", "retryable": True}))
    assert "provider fell over" in block.note_text and "upstream_error" in block.note_text and "send another message" in block.note_text
    block.update_from(assistant_entry("partial", status="error", error={"code": "context_length", "message": "too long", "retryable": False}))
    assert "send another message" not in block.note_text
    block.update_from(assistant_entry("partial", status="error", error=None))
    assert "could not finish" in block.note_text
    block.update_from(assistant_entry("partial\n\n[cancelled]", status="cancelled"))
    assert block.note_text == "Stopped."
    block.update_from(assistant_entry("fine"))
    assert block.note_text == "" and block._note.isHidden()


def test_tool_cards_follow_the_calls(qapp):
    block = AssistantBlock()
    block.update_from(assistant_entry("", calls=[("c1", "bash", {"command": "ls -la"}), ("c2", "read_file", {"path": "a.py"})]))
    assert list(block.cards) == ["c1", "c2"]
    assert "bash" in block.card("c1").header_text and "ls -la" in block.card("c1").header_text and "waiting" in block.card("c1").header_text
    block.update_from(assistant_entry("", calls=[("c1", "bash", {"command": "ls -la"}, [{"type": "text", "text": "total 0"}]),
                                                 ("c2", "read_file", {"path": "a.py"})]))
    assert "done" in block.card("c1").header_text and "waiting" in block.card("c2").header_text
    assert list(block.cards) == ["c1", "c2"]  # the same cards, updated


def test_tool_card_details_arguments_and_result(qapp):
    card = ToolCardWidget()
    call = t.ToolCallView("c1", "bash", {"command": "echo héllo"}, [{"type": "text", "text": "héllo\nsecond line"}])
    card.update_from(call)
    assert not card.expanded and card._details.isHidden()
    card.set_expanded(True)
    assert not card._details.isHidden() and card.header_text.startswith("▾")
    assert '"command": "echo héllo"' in card.details_text() and "héllo\nsecond line" in card.details_text()
    assert "Result" in card.details_text()
    card.set_expanded(False)
    assert card._details.isHidden() and card.header_text.startswith("▸")


def test_a_pending_tool_card_shows_arguments_only(qapp):
    card = ToolCardWidget()
    card.update_from(t.ToolCallView("c1", "bash", {"command": "ls"}))
    card.set_expanded(True)
    assert "Arguments" in card.details_text() and "Result" not in card.details_text()


def test_a_long_result_is_cut_until_show_all(qapp):
    card = ToolCardWidget()
    big = "line\n" * 3000
    card.update_from(t.ToolCallView("c1", "bash", {"command": "yes"}, [{"type": "text", "text": big}]))
    card.set_expanded(True)
    assert len(card.details_text()) < 6000 and not card._more.isHidden()
    card._more.click()
    assert len(card.details_text()) > 15000 and card._more.isHidden()


def test_tool_card_for_a_call_with_bad_arguments(qapp):
    card = ToolCardWidget()
    card.update_from(t.ToolCallView("c1", "bash", {"_invalid_arguments": "{oops"}))
    assert "not valid JSON" in card.header_text


# --- the transcript view -----------------------------------------------------------------------------------------------------------

def user_msg(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def assistant_msg(text, **kw):
    return {"role": "assistant", "text": {"type": "text", "text": text}, "reasoning": {"type": "text", "text": ""},
            "tool_calls": [], "status": kw.get("status", "complete"), "error": None}


@pytest.fixture
def view(qapp):
    v = shown(TranscriptView(), qapp, 500, 300)
    yield v
    v.close()


def kinds_of(view):
    return [type(b).__name__ for b in view.blocks_in_order()]


def test_blocks_follow_the_model_in_order(qapp, view):
    model = t.TranscriptModel()
    view.set_model(model)
    model.apply_committed(3, assistant_msg("third"))
    model.apply_committed(1, user_msg("first"))
    model.apply_committed(2, assistant_msg("second"))
    pump(qapp)
    assert kinds_of(view) == ["UserBlock", "AssistantBlock", "AssistantBlock"]
    assert [b.body_text() for b in view.blocks_in_order()] == ["first", "second", "third"]


def test_an_existing_model_is_drawn_at_once(qapp, view):
    model = t.TranscriptModel()
    model.apply_committed(1, user_msg("q"))
    model.apply_committed(2, assistant_msg("a"))
    view.set_model(model)
    assert kinds_of(view) == ["UserBlock", "AssistantBlock"]


def test_streaming_updates_are_applied_in_batches(qapp, view):
    model = t.TranscriptModel()
    view.set_model(model)
    for i in range(50):
        model.live_text("g1", f"w{i} ")
    assert view.blocks_in_order()[0].body_text() == ""  # nothing yet: the changes wait for the timer
    pump(qapp, 150)
    assert view.blocks_in_order()[0].body_text() == "".join(f"w{i} " for i in range(50)).strip()
    model.live_text("g1", "more")
    view.flush()
    assert view.blocks_in_order()[0].body_text().endswith("more")


def test_the_live_block_becomes_the_committed_one_without_flicker(qapp, view):
    model = t.TranscriptModel()
    view.set_model(model)
    model.apply_committed(1, user_msg("q"))
    model.live_text("g1", "Hel")
    pump(qapp, 100)
    live_block = view.blocks_in_order()[1]
    model.apply_committed(2, assistant_msg("Hello"), "g1")
    pump(qapp, 100)
    assert view.blocks_in_order()[1] is live_block and live_block.body_text() == "Hello" and len(view.blocks_in_order()) == 2


def test_a_pending_message_moves_into_place_when_committed(qapp, view):
    model = t.TranscriptModel()
    view.set_model(model)
    model.live_text("g1", "busy answer")
    key = model.add_pending_user([{"type": "text", "text": "queued question"}])
    model.mark_sent(key, None, queued=True)
    pump(qapp, 100)
    assert kinds_of(view) == ["AssistantBlock", "UserBlock"] and "queued" in view.blocks_in_order()[1].state_text
    model.apply_committed(1, user_msg("first"))
    model.apply_committed(2, assistant_msg("busy answer"), "g1")
    model.apply_committed(3, user_msg("queued question"))
    pump(qapp, 100)
    assert [b.body_text() for b in view.blocks_in_order()] == ["first", "busy answer", "queued question"]
    assert view.blocks_in_order()[2].state_text == ""


def test_removed_entries_lose_their_block(qapp, view):
    model = t.TranscriptModel()
    view.set_model(model)
    key = model.add_pending_user([{"type": "text", "text": "x"}])
    model.mark_failed(key, "no", unknown=False)
    pump(qapp)
    assert kinds_of(view) == ["UserBlock"]
    model.discard(key)
    pump(qapp)
    assert kinds_of(view) == [] and view.block(key) is None


def test_switching_models_rebuilds_and_detaches_from_the_old_one(qapp, view):
    first, second = t.TranscriptModel(), t.TranscriptModel()
    first.apply_committed(1, user_msg("in first"))
    second.apply_committed(1, user_msg("in second"))
    view.set_model(first)
    view.set_model(second)
    assert [b.body_text() for b in view.blocks_in_order()] == ["in second"]
    first.apply_committed(2, assistant_msg("late news"))  # the old model changes: no effect on the view
    pump(qapp)
    assert [b.body_text() for b in view.blocks_in_order()] == ["in second"]
    view.set_model(None)
    assert view.blocks_in_order() == []


def test_a_reset_redraws(qapp, view):
    model = t.TranscriptModel()
    view.set_model(model)
    model.apply_committed(1, user_msg("old"))
    model.reset()
    model.apply_committed(1, user_msg("new"))
    pump(qapp)
    assert [b.body_text() for b in view.blocks_in_order()] == ["new"]


def fill(model, view, qapp, count, start=1):
    for i in range(start, start + count):
        model.apply_committed(2 * i - 1, user_msg(f"question {i} " + "padding " * 10))
        model.apply_committed(2 * i, assistant_msg(f"answer {i} " + "text " * 40))
    pump(qapp, 80)


def test_the_view_follows_new_content_while_at_the_bottom(qapp, view):
    model = t.TranscriptModel()
    view.set_model(model)
    fill(model, view, qapp, 8)
    pump(qapp, 80)
    assert view.verticalScrollBar().maximum() > 0 and view.at_bottom and view.jump_button.isHidden()
    model.live_text("g9", "streaming " * 100)
    pump(qapp, 150)
    assert view.at_bottom


def test_scrolling_up_stops_following_and_offers_a_jump(qapp, view):
    model = t.TranscriptModel()
    view.set_model(model)
    fill(model, view, qapp, 8)
    view.verticalScrollBar().setValue(0)
    pump(qapp)
    assert not view.at_bottom
    fill(model, view, qapp, 2, start=9)  # new content arrives
    pump(qapp, 100)
    assert view.verticalScrollBar().value() == 0  # the reader's place is not taken away
    assert not view.jump_button.isHidden()
    view.jump_button.click()
    pump(qapp, 50)
    assert view.at_bottom and view.jump_button.isHidden()


def test_scrolling_back_to_the_bottom_resumes_following(qapp, view):
    model = t.TranscriptModel()
    view.set_model(model)
    fill(model, view, qapp, 8)
    view.verticalScrollBar().setValue(0)
    pump(qapp)
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    pump(qapp)
    assert view.jump_button.isHidden()
    fill(model, view, qapp, 2, start=9)
    pump(qapp, 100)
    assert view.at_bottom


# --- the composer ------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def composer(qapp):
    c = shown(Composer(), qapp, 500, 200)
    c.sent = []
    c.stops = []
    c.send_requested.connect(c.sent.append)
    c.stop_requested.connect(lambda: c.stops.append(1))
    yield c
    c.close()


def type_text(composer, text):
    QTest.keyClicks(composer.edit, text)


def test_ctrl_enter_sends_the_text_trimmed(qapp, composer):
    type_text(composer, "  hello world  ")
    QTest.keyClick(composer.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert composer.sent == ["hello world"]
    composer.set_text("via keypad")
    QTest.keyClick(composer.edit, Qt.Key.Key_Enter,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier)
    assert composer.sent[-1] == "via keypad"


@pytest.mark.parametrize(("key", "modifiers"), [
    (Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier),
    (Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier),
])
def test_enter_adds_a_line_and_ctrl_enter_sends(qapp, composer, key, modifiers):
    type_text(composer, "line one")
    QTest.keyClick(composer.edit, key, modifiers)
    type_text(composer, "line two")
    assert composer.sent == [] and composer.text() == "line one\nline two"
    QTest.keyClick(composer.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert composer.sent == ["line one\nline two"]


def test_the_text_stays_until_the_owner_clears_it(qapp, composer):
    type_text(composer, "keep me")
    QTest.keyClick(composer.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert composer.text() == "keep me"  # clearing is the pane's job, after it took the text
    composer.clear()
    assert composer.text() == ""


def test_empty_and_blank_text_is_not_sent(qapp, composer):
    QTest.keyClick(composer.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    composer.set_text("   \n  ")  # (QTest.keyClicks cannot type a newline)
    composer.send_button.click()
    assert composer.sent == []


def test_the_send_button_sends(qapp, composer):
    type_text(composer, "clicked")
    composer.send_button.click()
    assert composer.sent == ["clicked"]


def test_busy_turns_send_into_queue_and_enables_stop(qapp, composer):
    assert composer.send_button.text() == "Send" and not composer.stop_button.isEnabled()
    composer.set_busy(True)
    assert composer.send_button.text() == "Queue" and composer.stop_button.isEnabled()
    composer.stop_button.click()
    assert composer.stops == [1]
    type_text(composer, "next question")
    QTest.keyClick(composer.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert composer.sent == ["next question"]  # still possible while busy: it is queued by the backend
    composer.set_busy(False)
    assert composer.send_button.text() == "Send" and not composer.stop_button.isEnabled()


def test_sending_can_be_disallowed(qapp, composer):
    composer.set_sending_allowed(False, "This session cannot be used any more.")
    assert not composer.edit.isEnabled() and not composer.send_button.isEnabled()
    assert composer.edit.placeholderText() == "This session cannot be used any more."
    composer.set_text("typed anyway")
    composer.send_button.click()
    assert composer.sent == []
    composer.set_sending_allowed(True)
    assert composer.edit.isEnabled() and "Ctrl+Enter sends" in composer.edit.placeholderText()


def test_the_edit_grows_with_its_text_up_to_a_limit(qapp, composer):
    small = composer.edit.height()
    composer.set_text("\n".join(["x"] * 4))
    pump(qapp)
    four = composer.edit.height()
    composer.set_text("\n".join(["x"] * 40))
    pump(qapp)
    big = composer.edit.height()
    composer.set_text("\n".join(["x"] * 400))
    pump(qapp)
    assert small < four < big == composer.edit.height()
    line = composer.edit.fontMetrics().lineSpacing()
    assert big <= MAX_LINES * line + 30


def drag_resize_handle(qapp, composer, dy):
    handle = composer.resize_handle
    window = handle.window()
    start = handle.mapTo(window, handle.rect().center())
    end = start + QPoint(0, dy)
    # Use window coordinates: the handle follows the mouse when the composer grows or shrinks.
    QTest.mousePress(window.windowHandle(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(window.windowHandle(), end)
    QTest.mouseRelease(window.windowHandle(), Qt.MouseButton.LeftButton, pos=end)
    pump(qapp)


def test_the_resize_handle_can_shrink_a_long_draft_and_reset(qapp, composer):
    composer.resize(500, 350)
    edit = composer.edit
    small = edit.height()
    draft = "line of text\n" * 20
    composer.set_text(draft)
    pump(qapp)
    auto = edit.height()
    line = edit.fontMetrics().lineSpacing()
    handle = composer.resize_handle
    assert handle.isVisible() and handle.cursor().shape() == Qt.CursorShape.SizeVerCursor

    drag_resize_handle(qapp, composer, -2 * line)
    assert edit.height() == auto + 2 * line
    drag_resize_handle(qapp, composer, 4 * line)
    assert edit.height() == auto - 2 * line
    drag_resize_handle(qapp, composer, 10 * line)
    assert edit.height() == small
    assert composer.text() == draft and edit.verticalScrollBar().maximum() > 0

    composer.set_text(draft + "another line")
    pump(qapp)
    assert edit.height() == small  # manual sizing overrides auto-growth until reset
    QTest.mouseDClick(handle, Qt.MouseButton.LeftButton, pos=handle.rect().center())
    pump(qapp)
    assert edit.height() == auto and composer.text() == draft + "another line"


def test_resizing_the_composer_does_not_force_the_chat_window_taller(qapp):
    pane = shown(ChatPane(), qapp, 700, 480)
    original = pane.size()
    small = pane.composer.edit.height()
    drag_resize_handle(qapp, pane.composer, -original.height())
    tall = pane.composer.edit.height()
    assert tall > small and pane.size() == original
    assert pane.transcript.height() >= pane.transcript.minimumSizeHint().height()

    pane.resize(original.width(), original.height() - 120)
    pump(qapp)
    assert pane.height() == original.height() - 120
    assert small < pane.composer.edit.height() < tall
    assert pane.transcript.height() >= pane.transcript.minimumSizeHint().height()


def test_composer_is_a_plain_widget(qapp):
    assert isinstance(Composer(), QWidget)


# --- layout ---------------------------------------------------------------------------------------------------------------------------------

def test_an_unbreakable_word_wraps_instead_of_widening_the_view(qapp):
    view = shown(AutoHeightBrowser(), qapp, width=300)
    view.set_plain("x" * 400)
    assert view.height() > view.fontMetrics().lineSpacing() * 2 and view.width() == 300
    assert view.document().size().width() <= view.viewport().width() + 1


def test_blocks_fit_the_width_of_the_transcript(qapp, view):
    model = t.TranscriptModel()
    view.set_model(model)
    model.apply_committed(1, user_msg("a" * 300 + " and some more words " * 10))
    model.apply_committed(2, {**assistant_msg("https://example.com/" + "p" * 200 + "\n\n```\n" + "code " * 80 + "\n```"),
                              "tool_calls": [{"id": "c1", "name": "bash", "arguments": {"command": "echo " + "z" * 300}}]})
    pump(qapp, 100)
    viewport = view.viewport().width()
    for block in view.blocks_in_order():
        assert block.height() > 0 and block.width() <= viewport, (type(block).__name__, block.width(), viewport)
    assert view.horizontalScrollBar().maximum() == 0


def test_a_long_tool_header_is_cut_with_an_ellipsis(qapp):
    card = shown(ToolCardWidget(), qapp, width=260, height=80)
    card.update_from(t.ToolCallView("c1", "bash", {"command": "echo " + "z" * 300}))
    pump(qapp)
    shown_text = card._header.text()
    assert shown_text.endswith("…") and len(shown_text) < len(card.header_text)
    assert card.width() == 260 and card._header.width() <= 260
    card.resize(1400, 80)
    pump(qapp)
    assert len(card._header.text()) > len(shown_text)  # more room: more text


# --- the state of a tool call ------------------------------------------------------------------------------------------------------------------

def test_a_tool_card_shows_waiting_running_done_and_failed(qapp):
    card = ToolCardWidget()
    text = lambda: card.header_text  # noqa: E731
    call = t.ToolCallView("c1", "bash", {"command": "ls"})
    card.update_from(call)
    assert text().endswith("waiting")
    call.running = True
    card.update_from(call)
    assert text().endswith("running…")
    call.running, call.result, call.duration = False, [{"type": "text", "text": "<returncode>0</returncode>"}], 0.42
    card.update_from(call)
    assert text().endswith("done in 0.4 s")
    call.duration = None  # a result from the history: no stopwatch
    card.update_from(call)
    assert text().endswith("done")
    call.result = [{"type": "text", "text": "Error: file not found: x"}]
    call.duration = 12.34
    card.update_from(call)
    assert text().endswith("failed in 12.3 s")


def test_a_non_zero_exit_code_is_still_a_result_not_a_failure(qapp):
    card = ToolCardWidget()
    card.update_from(t.ToolCallView("c1", "bash", {"command": "false"}, [{"type": "text", "text": "<returncode>1</returncode>\n<stderr></stderr>\n<stdout></stdout>"}]))
    assert card.header_text.endswith("done")


def test_the_model_tracks_running_calls_and_their_results():
    model = t.TranscriptModel()
    changes = []
    model.live_tool_call("g", "c1", "bash", {"command": "x"})
    model.subscribe(changes.append)
    model.set_tool_running("c1")
    call = model.entries()[0].tool_calls[0]
    assert call.running and call.result is None and [c.kind for c in changes] == ["updated"]
    model.set_tool_result("c1", [{"type": "text", "text": "out"}], 1.5)
    assert not call.running and call.result and call.duration == 1.5
    model.set_tool_running("c1")  # a call that has its result is not running again
    assert not call.running
    model.set_tool_running("unknown")  # no error
    model.set_tool_result("c1", [{"type": "text", "text": "from the history"}])  # the backend's copy: the duration stays
    assert call.result[0]["text"] == "from the history" and call.duration == 1.5
