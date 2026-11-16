"""Model and option controls, the Continue shortcut (offscreen Qt, fake backend)."""
import json

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from etcode_desktop.core import controller as c  # noqa: E402
from etcode_desktop.core import session_state as ss  # noqa: E402
from etcode_desktop.core.models import ModelInfo, SessionMeta  # noqa: E402
from etcode_desktop.core.options import SessionOptions  # noqa: E402
from etcode_desktop.ui.model_bar import ModelBar  # noqa: E402
from etcode_desktop.ui.options_dialog import OptionsDialog, OptionsEditor  # noqa: E402
from fake_backend import Reply  # noqa: E402
from test_chat_pane import blocks, open_session, settled, texts, type_and_send  # noqa: E402
from test_gui_sessions import Env, wait_until  # noqa: E402

CODER = ModelInfo("Coder", "coder", 1000, True, "arena", True)
LITE = ModelInfo("Coder Lite", "lite", 1000, True, "arena", True)
TEXT = ModelInfo("Text", "text", 1000, False, "openrouter", True)
OFFLINE = ModelInfo("Offline", "offline", 1000, True, "openrouter", False)


def meta(model="coder", status="idle", websearch=False, options=None):
    return SessionMeta(id="s", model=model, status=status, websearch=websearch, options=options or {})


@pytest.fixture
def env(qapp, fake, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda parent, title, text, *a, **k: shown.append(text)))
    e = Env(qapp, fake, tmp_path)
    e.errors = shown
    fake.auto_reply = True
    yield e
    e.close()


# --- the bar on its own -------------------------------------------------------------------------------------------------------

def test_the_picker_lists_the_catalog_and_selects_the_sessions_model(qapp):
    bar = ModelBar()
    bar.set_models([CODER, LITE, TEXT])
    bar.set_session(meta("lite"), SessionOptions(), True)
    picker = bar.model_picker
    assert [picker.itemData(i) for i in range(picker.count())] == ["coder", "lite", "text"]
    assert picker.currentData() == "lite" and picker.isEnabled()
    assert "(images)" in picker.itemText(0) and "(images)" not in picker.itemText(2)


def test_models_that_cannot_be_chosen_are_greyed_out_and_say_why(qapp):
    bar = ModelBar()
    bar.set_models([CODER, LITE, TEXT, OFFLINE])
    bar.set_session(meta("coder"), SessionOptions(), True)
    assert bar.entry_state("coder") == (True, "")  # the one in use
    assert bar.entry_state("lite") == (True, "")
    can, why = bar.entry_state("text")
    assert not can and "current model takes pictures" in why
    can, why = bar.entry_state("offline")
    assert not can and "no API key" in why
    bar.set_session(meta("text"), SessionOptions(), True)
    can, why = bar.entry_state("coder")
    assert not can and "takes pictures, the current one does not" in why


def test_a_choice_is_reported_and_the_picker_goes_back_until_the_backend_agrees(qapp):
    bar = ModelBar()
    bar.set_models([CODER, LITE])
    bar.set_session(meta("coder"), SessionOptions(), True)
    chosen = []
    bar.model_chosen.connect(chosen.append)
    bar.model_picker.setCurrentIndex(1)
    bar.model_picker.activated.emit(1)
    assert chosen == ["lite"] and bar.model_picker.currentData() == "coder"
    bar.set_session(meta("lite"), SessionOptions(), True)  # the backend changed it
    assert bar.model_picker.currentData() == "lite"
    bar.model_picker.activated.emit(1)  # choosing the model that is in use is not a change
    assert chosen == ["lite"]


def test_the_picker_is_off_while_the_model_cannot_be_changed(qapp):
    bar = ModelBar()
    bar.set_models([CODER, LITE])
    bar.set_session(meta("coder", "generating"), SessionOptions(), False)
    assert not bar.model_picker.isEnabled() and "idle" in bar.model_picker.toolTip()
    bar.set_session(None, SessionOptions(), False)
    assert not bar.model_picker.isEnabled() and not bar.effort_picker.isEnabled() and not bar.options_button.isEnabled()


def test_a_model_that_the_catalog_does_not_list_is_still_shown(qapp):
    bar = ModelBar()
    bar.set_models([CODER, LITE])
    bar.set_session(meta("retired-model"), SessionOptions(), True)
    assert bar.model_picker.currentData() == "retired-model" and bar.model_picker.count() == 3
    bar.set_models(None)
    assert bar.model_picker.currentData() == "retired-model" and bar.model_picker.count() == 1


def test_web_search_is_shown_but_cannot_be_changed(qapp):
    bar = ModelBar()
    bar.set_session(meta(websearch=True), SessionOptions(), True)
    assert bar.web_indicator.isChecked() and not bar.web_indicator.isEnabled()
    bar.set_session(meta(websearch=False), SessionOptions(), True)
    assert not bar.web_indicator.isChecked()


def test_the_effort_picker_shows_and_reports_the_effort(qapp):
    bar = ModelBar()
    bar.set_session(meta(), SessionOptions(0.4, 100, "high"), True)
    picker = bar.effort_picker
    assert picker.currentData() == "high" and picker.isEnabled()
    heard = []
    bar.options_changed.connect(heard.append)
    picker.setCurrentIndex(picker.findData("low"))
    picker.activated.emit(picker.currentIndex())
    assert heard == [SessionOptions(0.4, 100, "low")]  # the other options are kept
    picker.setCurrentIndex(0)
    picker.activated.emit(0)
    assert heard[-1] == SessionOptions(0.4, 100, None)


def test_setting_the_session_does_not_count_as_a_choice(qapp):
    bar = ModelBar()
    heard = []
    bar.options_changed.connect(heard.append)
    bar.model_chosen.connect(heard.append)
    bar.set_models([CODER, LITE])
    bar.set_session(meta("coder"), SessionOptions(reasoning_effort="medium"), True)
    bar.set_session(meta("lite"), SessionOptions(reasoning_effort="low"), True)
    assert heard == []


def test_the_options_dialog_changes_the_options(qapp, monkeypatch):
    bar = ModelBar()
    bar.set_session(meta(), SessionOptions(reasoning_effort="low"), True)
    heard = []
    bar.options_changed.connect(heard.append)

    class Accepting(OptionsDialog):
        def exec(self):
            self._editor._temperature_on.setChecked(True)
            self._editor._temperature.setValue(0.3)
            self._editor._tokens_on.setChecked(True)
            self._editor._tokens.setValue(777)
            return OptionsDialog.DialogCode.Accepted

    monkeypatch.setattr("etcode_desktop.ui.model_bar.OptionsDialog", Accepting)
    bar.edit_options()
    assert heard == [SessionOptions(0.3, 777, "low")]

    class Rejecting(OptionsDialog):
        def exec(self):
            return OptionsDialog.DialogCode.Rejected

    monkeypatch.setattr("etcode_desktop.ui.model_bar.OptionsDialog", Rejecting)
    bar.edit_options()
    assert len(heard) == 1


def test_the_options_editor_round_trips_and_switches_fields_on_and_off(qapp):
    editor = OptionsEditor()
    assert editor.options() == SessionOptions()
    assert not editor._temperature.isEnabled() and not editor._tokens.isEnabled()
    editor.set_options(SessionOptions(1.25, 2048, "xhigh"))
    assert editor.options() == SessionOptions(1.25, 2048, "xhigh") and editor._temperature.isEnabled()
    editor.set_options(SessionOptions())
    assert editor.options() == SessionOptions() and not editor._tokens_on.isChecked()


# --- in a conversation -----------------------------------------------------------------------------------------------------------

def test_the_bar_shows_the_model_and_the_options_of_the_session(env, qapp, fake):
    session = fake.add_session(model="fake-coder-lite", websearch=True, options={"reasoning_effort": "high", "temperature": 0.2})
    window, pane, session, store, bridge = open_session(env, qapp, fake, session)
    assert wait_until(qapp, lambda: bridge.models is not None and pane.model_bar.model_picker.count() == 3)
    bar = pane.model_bar
    assert bar.model_picker.currentData() == "fake-coder-lite" and bar.model_picker.isEnabled()
    assert bar.effort_picker.currentData() == "high" and bar.web_indicator.isChecked()
    assert pane.chat.options == SessionOptions(temperature=0.2, reasoning_effort="high")
    assert bar.entry_state("fake-text")[0] is False  # the backend would refuse that change


def test_changing_the_model_works_and_the_next_message_uses_it(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    assert wait_until(qapp, lambda: pane.model_bar.model_picker.count() == 3)
    picker = pane.model_bar.model_picker
    picker.setCurrentIndex(picker.findData("fake-coder-lite"))
    picker.activated.emit(picker.currentIndex())
    assert wait_until(qapp, lambda: session.model == "fake-coder-lite")
    assert wait_until(qapp, lambda: pane.model_bar.model_picker.currentData() == "fake-coder-lite")
    assert store.get(bridge.scope, session.id).model == "fake-coder-lite"


def test_a_change_that_the_backend_refuses_is_reported_and_the_picker_shows_the_real_model(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    assert wait_until(qapp, lambda: pane.model_bar.model_picker.count() == 3)
    fake.fail_next("PATCH", "/model$", status=409, code="session_busy", message="Cannot change model during generation")
    picker = pane.model_bar.model_picker
    picker.setCurrentIndex(picker.findData("fake-coder-lite"))
    picker.activated.emit(picker.currentIndex())
    assert wait_until(qapp, lambda: ss.NOTICE_ERROR in pane.notice_kinds())
    assert "The model could not be changed" in pane.notice_text(ss.NOTICE_ERROR) and "Cannot change model during generation" in pane.notice_text(ss.NOTICE_ERROR)
    assert wait_until(qapp, lambda: pane.model_bar.model_picker.currentData() == "fake-coder")
    assert session.model == "fake-coder"


def test_the_picker_is_off_while_the_model_works(env, qapp, fake):
    fake.replies.append(Reply(text="x" * 60, chunks=60, delay=0.05))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    assert wait_until(qapp, lambda: pane.model_bar.model_picker.count() == 3 and pane.model_bar.model_picker.isEnabled())
    type_and_send(pane, "go")
    assert wait_until(qapp, lambda: pane.chat.state.busy)
    assert wait_until(qapp, lambda: not pane.model_bar.model_picker.isEnabled())
    pane.composer.stop_button.click()
    assert wait_until(qapp, lambda: settled(pane, fake, session) and pane.model_bar.model_picker.isEnabled(), 10)


def test_the_options_go_with_every_message_and_are_remembered(env, qapp, fake):
    fake.replies.extend([Reply(text="one"), Reply(text="two")])
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    assert wait_until(qapp, lambda: pane.model_bar.effort_picker.isEnabled())
    pane.chat.set_options(SessionOptions(0.6, 321, "medium"))
    type_and_send(pane, "first")
    assert wait_until(qapp, lambda: settled(pane, fake, session))
    body = fake.requests_to("POST", "/messages$")[-1].json
    assert (body["temperature"], body["max_tokens"], body["reasoning_effort"]) == (0.6, 321, "medium")
    # the choice outlives the conversation object: it was saved for this session
    window.close()
    again, pane2, _, _, _ = open_session(env, qapp, fake, session)
    assert pane2.chat.options == SessionOptions(0.6, 321, "medium")
    assert pane2.model_bar.effort_picker.currentData() == "medium"


def test_no_options_send_nothing_so_the_backends_defaults_apply(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "plain")
    assert wait_until(qapp, lambda: settled(pane, fake, session))
    body = fake.requests_to("POST", "/messages$")[-1].json
    assert set(body) == {"messages"}


def test_a_queued_message_is_sent_with_the_options_too(env, qapp, fake):
    fake.replies.extend([Reply(text="a" * 20, chunks=20, delay=0.05), Reply(text="b")])
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    pane.chat.set_options(SessionOptions(reasoning_effort="low"))
    type_and_send(pane, "one")
    assert wait_until(qapp, lambda: pane.chat.state.busy)
    pane.chat.set_options(SessionOptions(reasoning_effort="high"))
    type_and_send(pane, "two")
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    efforts = [r.json["reasoning_effort"] for r in fake.requests_to("POST", "/messages$")]
    assert efforts == ["low", "high"]


def test_options_of_a_session_come_from_the_backend_until_the_user_changes_them(env, qapp, fake):
    session = fake.add_session(options={"temperature": 0.9, "max_tokens": None, "reasoning_effort": None})
    window, pane, session, store, bridge = open_session(env, qapp, fake, session)
    assert pane.chat.options == SessionOptions(temperature=0.9)
    pane.chat.set_options(SessionOptions(max_tokens=50))
    assert pane.chat.options == SessionOptions(max_tokens=50)  # what the user chose replaces it


def test_a_new_session_can_be_created_with_options(env, qapp, fake, monkeypatch):
    from etcode_desktop.ui import main_window as mw
    from test_gui_sessions import StubDialog

    class WithOptions(StubDialog):
        def options(self):
            return SessionOptions(0.25, 900, "low")

    monkeypatch.setattr(mw, "NewSessionDialog", WithOptions)
    WithOptions.choice, WithOptions.seen = ("fake-coder", False), []
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: bridge.models is not None)
    window.request_new_session()
    assert wait_until(qapp, lambda: len(fake.sessions) == 1)
    created = next(iter(fake.sessions.values()))
    assert created.options == {"temperature": 0.25, "max_tokens": 900, "reasoning_effort": "low"}
    assert json.loads(store.kv_get("last_options")) == {"max_tokens": 900, "reasoning_effort": "low", "temperature": 0.25}


def test_the_new_session_dialog_has_the_option_fields_and_remembers_them(qapp):
    from etcode_desktop.ui.new_session_dialog import NewSessionDialog
    dialog = NewSessionDialog([CODER, TEXT], "coder", None, SessionOptions(0.5, 100, "high"))
    assert dialog.options() == SessionOptions(0.5, 100, "high")
    assert NewSessionDialog([CODER]).options() == SessionOptions()


# --- Continue ------------------------------------------------------------------------------------------------------------------------

def test_can_continue_in_the_state():
    state = ss.SessionState()
    assert not state.can_continue
    error = {"role": "assistant", "text": {"type": "text", "text": "half"}, "reasoning": {"type": "text", "text": ""},
             "tool_calls": [], "status": "error", "error": {"code": "x", "message": "bad"}}
    fine = dict(error, status="complete", error=None)
    from etcode_desktop.core.models import HistoryEntry
    state.apply(c.HistoryLoaded((HistoryEntry(1, {"role": "user", "content": [{"type": "text", "text": "q"}]}), HistoryEntry(2, error)),
                                meta(status="failed")))
    assert state.can_continue
    state.apply(c.StatusChanged(meta(status="generating")))
    assert not state.can_continue  # busy
    state.apply(c.StatusChanged(meta(status="idle")))
    assert state.can_continue
    state.apply(c.Unavailable("gone"))
    assert not state.can_continue
    ok = ss.SessionState()
    ok.apply(c.HistoryLoaded((HistoryEntry(2, fine),), meta()))
    assert not ok.can_continue


def test_continue_appears_after_a_failed_answer_and_asks_the_model_to_go_on(env, qapp, fake):
    fake.replies.extend([Reply(text="half an answer", error={"code": "upstream_error", "message": "provider fell over", "retryable": True}),
                         Reply(text="and the rest")])
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    assert pane.continue_button.isHidden()
    type_and_send(pane, "write the essay")
    assert wait_until(qapp, lambda: not pane.continue_button.isHidden())
    pane.continue_button.click()
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    assert pane.continue_button.isHidden()
    assert [t for k, t in texts(pane) if k == "UserBlock"] == ["write the essay", "Please continue."]
    assert texts(pane)[-1] == ("AssistantBlock", "and the rest")


def test_continue_is_not_offered_after_a_good_answer_or_while_busy(env, qapp, fake):
    fake.replies.append(Reply(text="x" * 40, chunks=40, delay=0.05))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "go")
    assert wait_until(qapp, lambda: pane.chat.state.busy)
    assert pane.continue_button.isHidden()
    pane.composer.stop_button.click()
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    assert pane.continue_button.isHidden() and len(blocks(pane)) == 2  # a stopped answer is not an error
