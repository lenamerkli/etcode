"""The conversation in the main window, against the scriptable fake backend (offscreen Qt)."""
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QMessageBox, QPushButton  # noqa: E402

from etcode_desktop.core import controller as c  # noqa: E402
from etcode_desktop.core import session_state as ss  # noqa: E402
from fake_backend import Reply, StreamFault  # noqa: E402
from test_gui_sessions import Env, wait_until  # noqa: E402


@pytest.fixture
def env(qapp, fake, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda parent, title, text, *a, **k: shown.append(text)))
    e = Env(qapp, fake, tmp_path)
    e.errors = shown
    fake.auto_reply = True
    yield e
    e.close()


def open_session(env, qapp, fake, session=None):
    session = session or fake.add_session()
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: session.id in window.sidebar.ids())
    window.sidebar.select(session.id)
    pane = window.pane
    assert wait_until(qapp, lambda: pane.chat.state.loaded)
    return window, pane, session, store, bridge


def blocks(pane):
    pane.transcript.flush()
    return pane.transcript.blocks_in_order()


def texts(pane):
    return [(type(b).__name__, b.body_text()) for b in blocks(pane)]


def card_text(pane, index, call_id):
    """The header of a tool card, or '' while the block or the card is not there yet (a wait must not break on that)."""
    shown_blocks = blocks(pane)
    if len(shown_blocks) <= index or not hasattr(shown_blocks[index], "card"):
        return ""
    card = shown_blocks[index].card(call_id)
    return card.header_text if card is not None else ""


def type_and_send(pane, text):
    pane.composer.edit.setFocus()
    QTest.keyClicks(pane.composer.edit, text)
    QTest.keyClick(pane.composer.edit, Qt.Key.Key_Return)


def settled(pane, fake, session):
    return settled_chat(pane.chat, session, fake)


# --- a conversation ---------------------------------------------------------------------------------------------------

def test_typing_a_message_shows_it_and_the_streamed_answer(env, qapp, fake):
    fake.replies.append(Reply(text="Hello there, friend!", reasoning="thinking about it", chunks=5, delay=0.03, cost="0.25"))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    assert pane.status_text == "idle"
    type_and_send(pane, "Say hello")
    assert pane.composer.text() == ""  # the box is free for the next message
    assert wait_until(qapp, lambda: settled(pane, fake, session))
    assert texts(pane) == [("UserBlock", "Say hello"), ("AssistantBlock", "Hello there, friend!")]
    assistant = blocks(pane)[1]
    assert assistant.thinking_visible and assistant.reasoning_shown_text() == "thinking about it"
    assert blocks(pane)[0].state_text == ""
    assert pane.status_text == "idle" and pane.cost_text == "$0.25"
    assert pane.composer.send_button.text() == "Send" and not pane.composer.stop_button.isEnabled()


def test_the_answer_appears_piece_by_piece(env, qapp, fake):
    fake.replies.append(Reply(text="".join(f"w{i} " for i in range(12)), chunks=12, delay=0.08, start_delay=0.1))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "stream please")
    seen = set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not settled(pane, fake, session):
        qapp.processEvents()
        pane.transcript.flush()
        for b in pane.transcript.blocks_in_order():
            if type(b).__name__ == "AssistantBlock":
                seen.add(len(b.body_text()))
        time.sleep(0.01)
    assert len(seen) >= 3, f"the text should have grown in steps, saw lengths {sorted(seen)}"


def test_while_the_model_works_the_composer_says_queue_and_stop_works(env, qapp, fake):
    fake.replies.append(Reply(text="x" * 80, chunks=80, delay=0.05))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "write a lot")
    assert wait_until(qapp, lambda: pane.composer.send_button.text() == "Queue" and pane.composer.stop_button.isEnabled())
    assert pane.status_text == "working"
    assert wait_until(qapp, lambda: len(blocks(pane)) == 2 and len(blocks(pane)[1].body_text()) > 0)
    pane.composer.stop_button.click()
    assert wait_until(qapp, lambda: settled(pane, fake, session))
    assistant = blocks(pane)[1]
    assert assistant.note_text == "Stopped." and 0 < len(assistant.body_text().rstrip()) < 120
    assert pane.composer.send_button.text() == "Send"


def test_escape_stops_too_but_only_when_something_runs(env, qapp, fake):
    fake.replies.append(Reply(text="y" * 60, chunks=60, delay=0.05))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    pane._on_stop()  # nothing runs: nothing happens
    time.sleep(0.1)
    qapp.processEvents()
    assert not fake.requests_to("POST", "/cancel$")
    type_and_send(pane, "go")
    assert wait_until(qapp, lambda: pane.chat.state.busy)
    pane._on_stop()
    assert wait_until(qapp, lambda: settled(pane, fake, session))
    assert fake.requests_to("POST", "/cancel$")


def test_a_message_typed_while_busy_is_shown_as_queued_and_then_answered(env, qapp, fake):
    fake.replies.extend([Reply(text="first answer", chunks=6, delay=0.08), Reply(text="second answer")])
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "one")
    assert wait_until(qapp, lambda: pane.composer.send_button.text() == "Queue")
    type_and_send(pane, "two")
    assert wait_until(qapp, lambda: any("queued" in getattr(b, "state_text", "") for b in blocks(pane)))
    assert "1 message queued" in pane.status_text or "queued" in pane.status_text
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    assert texts(pane) == [("UserBlock", "one"), ("AssistantBlock", "first answer"), ("UserBlock", "two"), ("AssistantBlock", "second answer")]
    assert all(getattr(b, "state_text", "") == "" for b in blocks(pane))


def test_a_refused_message_stays_visible_as_not_sent(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    fake.fail_next("POST", "/messages$", status=400, code="validation_error", message="content must not be empty")
    type_and_send(pane, "hello")
    assert wait_until(qapp, lambda: any("not sent" in getattr(b, "state_text", "") for b in blocks(pane)))
    assert "content must not be empty" in blocks(pane)[0].state_text
    assert blocks(pane)[0].body_text() == "hello"


def test_the_title_in_the_sidebar_follows_the_first_message(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    assert env.titles(window) == ["New session"]
    type_and_send(pane, "Fix the build")
    assert wait_until(qapp, lambda: env.titles(window) == ["Fix the build"])


def test_a_failed_answer_is_shown_with_its_error(env, qapp, fake):
    fake.replies.append(Reply(text="half an ans", error={"code": "upstream_error", "message": "provider fell over", "retryable": True}))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "go")
    assert wait_until(qapp, lambda: len(blocks(pane)) == 2 and "provider fell over" in blocks(pane)[1].note_text)
    assert blocks(pane)[1].body_text() == "half an ans" and pane.status_text == "failed"
    assert wait_until(qapp, lambda: pane.composer.send_button.isEnabled())


def test_tool_calls_are_shown_as_cards_and_stop_cancels_them(env, qapp, fake):
    fake.replies.append(Reply(text="Let me look.", start_delay=0.1, tool_calls=[("c1", "bash", {"command": "ls -la"})]))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "what is here?")
    assert wait_until(qapp, lambda: ss.NOTICE_TOOLS in pane.notice_kinds())
    assistant = blocks(pane)[1]
    card = assistant.card("c1")
    assert "bash" in card.header_text and "ls -la" in card.header_text and "waiting" in card.header_text
    assert "cannot run them" not in pane.notice_text(ss.NOTICE_TOOLS) and "Stop" in pane.notice_text(ss.NOTICE_TOOLS)
    assert pane.status_text == "running tools" and pane.composer.stop_button.isEnabled()
    pane.composer.stop_button.click()
    assert wait_until(qapp, lambda: ss.NOTICE_TOOLS not in pane.notice_kinds() and pane.status_text == "idle")
    assert wait_until(qapp, lambda: "done" in card_text(pane, 1, "c1"))
    card = blocks(pane)[1].card("c1")
    card.set_expanded(True)
    assert "cancelled" in card.details_text().lower()


def test_a_web_search_card_gets_its_result(env, qapp, fake):
    fake.replies.extend([Reply(text="Searching.", start_delay=0.1, tool_calls=[("w1", "websearch", {"query": "python docs"})],
                               search_result="1. docs.python.org"), Reply(text="Here is what I found.")])
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "find python docs")
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    assistant_blocks = [b for b in blocks(pane) if type(b).__name__ == "AssistantBlock"]
    assert len(assistant_blocks) == 2
    card = assistant_blocks[0].card("w1")
    assert "websearch" in card.header_text and "python docs" in card.header_text and "done" in card.header_text
    card.set_expanded(True)
    assert "docs.python.org" in card.details_text()
    assert assistant_blocks[1].body_text() == "Here is what I found."
    assert ss.NOTICE_TOOLS not in pane.notice_kinds()


def test_the_history_of_a_session_is_shown_when_it_is_opened(env, qapp, fake):
    session = fake.add_session()
    session.messages += [
        {"role": "user", "content": [{"type": "text", "text": "old question"}]},
        {"role": "assistant", "text": {"type": "text", "text": "old **answer**"}, "reasoning": {"type": "text", "text": "old thoughts"},
         "tool_calls": [{"id": "c1", "name": "bash", "arguments": {"command": "pwd"}}], "status": "complete", "error": None},
        {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "/home/agent"}]},
        {"role": "assistant", "text": {"type": "text", "text": "done"}, "reasoning": {"type": "text", "text": ""},
         "tool_calls": [], "status": "complete", "error": None},
    ]
    session.cost_exact = "0.5"
    window, pane, session, store, bridge = open_session(env, qapp, fake, session)
    assert texts(pane) == [("UserBlock", "old question"), ("AssistantBlock", "old answer"), ("AssistantBlock", "done")]
    card = blocks(pane)[1].card("c1")
    assert "done" in card.header_text
    card.set_expanded(True)
    assert "/home/agent" in card.details_text()
    assert pane.cost_text == "$0.5"


# --- several sessions ---------------------------------------------------------------------------------------------------------

def test_a_session_keeps_working_while_another_one_is_shown(env, qapp, fake):
    first, second = fake.add_session(), fake.add_session()
    fake.replies.append(Reply(text="".join(f"p{i} " for i in range(10)), chunks=10, delay=0.08))
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: len(window.sidebar.ids()) == 2)
    window.sidebar.select(first.id)
    pane = window.pane
    assert wait_until(qapp, lambda: pane.chat.state.loaded)
    type_and_send(pane, "work for a while")
    assert wait_until(qapp, lambda: pane.chat.state.busy)
    chat_one = pane.chat
    window.sidebar.select(second.id)
    assert wait_until(qapp, lambda: pane.chat is not chat_one and pane.chat.state.loaded)
    assert texts(pane) == []  # the other session is empty
    assert wait_until(qapp, lambda: settled_chat(chat_one, first, fake), 10)  # the first one finished in the background
    window.sidebar.select(first.id)
    assert pane.chat is chat_one
    assert wait_until(qapp, lambda: [t for k, t in texts(pane)][-1] == "".join(f"p{i} " for i in range(10)).strip())


def settled_chat(chat, session, fake):
    """Nothing is being sent, the backend runs no generation, and everything it committed is shown."""
    state = chat.state
    return (state.status == "idle" and session.status == "idle" and state.meta.message_count == len(session.messages)
            and set(range(len(session.messages))) <= state.model.seen_indexes and not chat.controller.is_following
            and all(g.finished for g in fake.generations if g.session is session)
            and not any(getattr(e, "state", "") == "sending" for e in state.model.entries()))


def test_switching_the_scope_closes_the_open_conversations(env, qapp, fake):
    mine = fake.add_session(owner="tok-a")
    window, pane, session, store, bridge = open_session(env, qapp, fake, mine)
    chat = pane.chat
    bridge.reconfigure(env.settings(token="tok-b"))
    assert pane.chat is None and window.shown_session_id is None
    assert chat.controller._closed.is_set()


# --- notices ---------------------------------------------------------------------------------------------------------------------------

def test_a_failed_load_offers_to_try_again(env, qapp, fake):
    session = fake.add_session()
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: session.id in window.sidebar.ids())
    fake.fail_next("GET", f"/session/{session.id}$", status=503, times=10)
    window.sidebar.select(session.id)
    pane = window.pane
    assert wait_until(qapp, lambda: ss.NOTICE_LOAD in pane.notice_kinds())
    assert pane.status_text == "Could not load"
    fake.rest_faults.clear()
    retry = pane.notice_frame(ss.NOTICE_LOAD).findChild(QPushButton, "retryButton")
    retry.click()
    assert wait_until(qapp, lambda: pane.chat.state.loaded and ss.NOTICE_LOAD not in pane.notice_kinds())
    assert pane.status_text == "idle"


def test_a_session_that_is_gone_disables_the_composer(env, qapp, fake):
    session = fake.add_session()
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: session.id in window.sidebar.ids())
    del fake.sessions[session.id]
    window.sidebar.select(session.id)
    pane = window.pane
    assert wait_until(qapp, lambda: ss.NOTICE_UNAVAILABLE in pane.notice_kinds())
    assert not pane.composer.edit.isEnabled() and not pane.composer.send_button.isEnabled()
    assert "not on the backend" in pane.notice_text(ss.NOTICE_UNAVAILABLE)
    assert wait_until(qapp, lambda: store.get(bridge.scope, session.id).is_missing)


def test_a_lost_connection_shows_a_countdown_and_goes_away_when_it_is_back(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    state = pane.chat.state
    state.apply(c.ConnectionLost("cannot connect to http://x", 30.0))
    assert ss.NOTICE_CONNECTION in pane.notice_kinds()
    text = pane.notice_text(ss.NOTICE_CONNECTION)
    assert "cannot connect to http://x" in text and "Trying again in" in text and "s." in text
    state.apply(c.ConnectionLost("cannot connect to http://x", 0.0))
    assert "Trying again…" in pane.notice_text(ss.NOTICE_CONNECTION)
    state.apply(c.ConnectionRestored())
    assert ss.NOTICE_CONNECTION not in pane.notice_kinds()


def test_the_countdown_counts_down(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    pane.chat.state.apply(c.ConnectionLost("down", 3.0))
    first = pane.notice_text(ss.NOTICE_CONNECTION)
    assert wait_until(qapp, lambda: pane.notice_text(ss.NOTICE_CONNECTION) != first, 3.0)


def test_an_action_error_can_be_dismissed(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    pane.chat.state.apply(c.ActionFailed("Could not stop: it broke"))
    assert "Could not stop" in pane.notice_text(ss.NOTICE_ERROR)
    pane.notice_frame(ss.NOTICE_ERROR).findChild(QPushButton, "dismissButton").click()
    assert ss.NOTICE_ERROR not in pane.notice_kinds()


def test_notices_belong_to_their_session(env, qapp, fake):
    first, second = fake.add_session(), fake.add_session()
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: len(window.sidebar.ids()) == 2)
    window.sidebar.select(first.id)
    pane = window.pane
    assert wait_until(qapp, lambda: pane.chat.state.loaded)
    pane.chat.state.apply(c.ActionFailed("only for the first"))
    assert ss.NOTICE_ERROR in pane.notice_kinds()
    window.sidebar.select(second.id)
    assert wait_until(qapp, lambda: pane.chat.state.loaded)
    assert pane.notice_kinds() == []
    window.sidebar.select(first.id)
    assert ss.NOTICE_ERROR in pane.notice_kinds()


# --- connection trouble, end to end -------------------------------------------------------------------------------------------------------

def test_a_dropped_stream_is_invisible_in_the_final_text(env, qapp, fake):
    text = "".join(f"{i}," for i in range(14))
    fake.replies.append(Reply(text=text, chunks=14, delay=0.04))
    fake.stream_faults.append(StreamFault(cut_after=3, abrupt=True))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "go")
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    assert texts(pane)[-1] == ("AssistantBlock", text.strip())
    assert ss.NOTICE_CONNECTION not in pane.notice_kinds()


def test_closing_the_window_stops_everything_quietly(env, qapp, fake):
    fake.replies.append(Reply(text="z" * 200, chunks=200, delay=0.05))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "long one")
    assert wait_until(qapp, lambda: pane.chat.state.busy)
    chat = pane.chat
    window.close()
    assert chat.controller._closed.is_set()
    assert wait_until(qapp, lambda: not chat.controller.is_following, 3.0)


def test_the_escape_key_stops_the_model(env, qapp, fake):
    fake.replies.append(Reply(text="y" * 80, chunks=80, delay=0.05))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    window.activateWindow()
    type_and_send(pane, "go")
    assert wait_until(qapp, lambda: pane.chat.state.busy and len(blocks(pane)) == 2)
    QTest.keyClick(pane.composer.edit, Qt.Key.Key_Escape)
    assert wait_until(qapp, lambda: fake.requests_to("POST", "/cancel$"), 3.0), "Esc did not reach the Stop shortcut"
    assert wait_until(qapp, lambda: settled(pane, fake, session))


# --- running tools in the window -----------------------------------------------------------------------------------------------------------------

from etcode_desktop.core.tools.scheduler import ToolPool, ToolRunner  # noqa: E402


class SlowStub:
    def __init__(self, seconds=0.0, text="stub result"):
        self.seconds, self.text, self.calls = seconds, text, []

    def __call__(self, name, arguments, context):
        self.calls.append((name, arguments))
        if self.seconds:
            context.cancel.wait(self.seconds)
        return [{"type": "text", "text": self.text}]


def open_with_tools(env, qapp, fake, execute, session=None):
    runner = ToolRunner(ToolPool(4), execute=execute)
    session = session or fake.add_session()
    window, bridge, store = env.open(tools=runner)
    assert wait_until(qapp, lambda: session.id in window.sidebar.ids())
    window.sidebar.select(session.id)
    assert wait_until(qapp, lambda: window.pane.chat.state.loaded)
    return window, window.pane, session, store, bridge


def test_a_tool_call_runs_and_its_card_goes_from_running_to_done(env, qapp, fake):
    stub = SlowStub(seconds=0.5, text="the output")
    fake.replies.extend([Reply(text="Running it.", start_delay=0.1, tool_calls=[("c1", "bash", {"command": "ls"})]), Reply(text="It worked.")])
    window, pane, session, store, bridge = open_with_tools(env, qapp, fake, stub)
    type_and_send(pane, "run ls")
    assert wait_until(qapp, lambda: "running" in card_text(pane, 1, "c1"))
    assert pane.status_text == "running tools" and pane.composer.stop_button.isEnabled()
    assert ss.NOTICE_TOOLS not in pane.notice_kinds()  # nothing to complain about: the call is being run
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    blocks_now = blocks(pane)
    card = [b for b in blocks_now if type(b).__name__ == "AssistantBlock"][0].card("c1")
    assert "done in" in card.header_text
    card.set_expanded(True)
    assert "the output" in card.details_text()
    assert [b.body_text() for b in blocks_now if type(b).__name__ == "AssistantBlock"][-1] == "It worked."
    assert stub.calls == [("bash", {"command": "ls"})]


def test_a_failing_call_is_marked_failed(env, qapp, fake):
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[("c1", "read_file", {"path": "missing.txt"})]), Reply(text="ok")])
    window, pane, session, store, bridge = open_with_tools(env, qapp, fake, SlowStub(text="Error: file not found: missing.txt"))
    type_and_send(pane, "read it")
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    card = [b for b in blocks(pane) if type(b).__name__ == "AssistantBlock"][0].card("c1")
    assert "failed" in card.header_text


def test_stop_ends_a_running_call_and_the_window_goes_idle(env, qapp, fake):
    stub = SlowStub(seconds=30)
    fake.replies.append(Reply(text="x", start_delay=0.1, tool_calls=[("c1", "bash", {"command": "sleep 99"})]))
    window, pane, session, store, bridge = open_with_tools(env, qapp, fake, stub)
    type_and_send(pane, "wait for it")
    assert wait_until(qapp, lambda: "running" in card_text(pane, 1, "c1"))
    started = time.monotonic()
    pane.composer.stop_button.click()
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    assert time.monotonic() - started < 6
    assert pane.status_text == "idle"
    card = [b for b in blocks(pane) if type(b).__name__ == "AssistantBlock"][0].card("c1")
    assert "running" not in card.header_text


# --- picking up sessions that a previous run left busy ------------------------------------------------------------------------------------------------

def awaiting(fake, calls):
    session = fake.add_session()
    session.status = "awaiting_tools"
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "earlier request"}]})
    session.messages.append({"role": "assistant", "text": {"type": "text", "text": "on it"}, "reasoning": {"type": "text", "text": ""},
                             "tool_calls": [{"id": i, "name": n, "arguments": a} for i, n, a in calls], "status": "complete", "error": None})
    session.pending = [{"id": i, "name": n, "arguments": a} for i, n, a in calls]
    return session


def test_a_session_that_waited_for_tools_when_the_app_started_is_continued_without_opening_it(env, qapp, fake):
    stub = SlowStub(text="finished while nobody looked")
    session = awaiting(fake, [("c1", "bash", {"command": "echo hi"})])
    fake.replies.append(Reply(text="all done"))
    runner = ToolRunner(ToolPool(4), execute=stub)
    window, bridge, store = env.open(tools=runner)
    assert window.shown_session_id is None  # nobody selected it
    assert wait_until(qapp, lambda: session.status == "idle" and session.messages[-1]["role"] == "assistant", 10)
    assert stub.calls == [("bash", {"command": "echo hi"})]
    assert [m["content"][0]["text"] for m in session.messages if m["role"] == "tool"] == ["finished while nobody looked"]
    assert session.messages[-1]["text"]["text"] == "all done"


def test_only_busy_sessions_are_resumed_and_only_once(env, qapp, fake):
    busy = awaiting(fake, [("c1", "bash", {"command": "x"})])
    idle = fake.add_session()
    gone = fake.add_session()
    fake.replies.append(Reply(text="done"))
    window, bridge, store = env.open(tools=ToolRunner(ToolPool(2), execute=SlowStub()), interval=80)
    assert wait_until(qapp, lambda: busy.status == "idle" and busy.messages[-1]["role"] == "assistant", 10)
    time.sleep(0.4)  # several more refreshes
    qapp.processEvents()
    assert window.chat_for_test(busy.id) is not None
    assert window.chat_for_test(idle.id) is None and window.chat_for_test(gone.id) is None
    assert len(fake.requests_to("GET", f"/session/{busy.id}/messages$")) == 1  # opened once, not on every refresh
    assert not fake.requests_to("GET", f"/session/{idle.id}/messages$")


def test_a_generation_that_was_running_is_followed_after_a_restart(env, qapp, fake):
    session = fake.add_session()
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "question"}]})
    fake.replies.append(Reply(text="a" * 30, chunks=30, delay=0.05))
    fake._maybe_reply(session)
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: window.chat_for_test(session.id) is not None)
    chat = window.chat_for_test(session.id)
    assert wait_until(qapp, lambda: chat.state.loaded and chat.state.model.entries() and chat.state.model.entries()[-1].text == "a" * 30, 10)
