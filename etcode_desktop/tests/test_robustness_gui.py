"""Outages as the user sees them: the status bar, the notices, and what happens by itself when the backend is back."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton  # noqa: E402

from etcode_desktop.core import controller as c  # noqa: E402
from etcode_desktop.core import session_state as ss  # noqa: E402
from etcode_desktop.ui import bridge as states  # noqa: E402
from fake_backend import Reply  # noqa: E402
from test_chat_pane import blocks, open_session, settled, texts, type_and_send  # noqa: E402
from test_gui_sessions import Env, wait_until  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(qapp, fake, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda parent, title, text, *a, **k: shown.append(text)))
    e = Env(qapp, fake, tmp_path)
    e.errors = shown
    fake.auto_reply = True
    yield e
    e.close()


def state_text(window):
    return window.findChild(QLabel, "stateLabel").text()


def retry_button(window):
    return window.findChild(QPushButton, "retryConnectionButton")


# --- the status bar --------------------------------------------------------------------------------------------------------------------

def test_an_unreachable_backend_shows_a_countdown_and_a_retry_button(env, qapp, fake):
    fake.go_down()
    window, bridge, store = env.open(interval=60_000)
    assert wait_until(qapp, lambda: bridge.state == states.OFFLINE)
    assert "unreachable" in state_text(window) and "trying again in" in state_text(window)
    assert not retry_button(window).isHidden()
    assert bridge.seconds_until_refresh() is not None


def test_the_countdown_goes_down(env, qapp, fake):
    fake.go_down()
    window, bridge, _ = env.open(interval=60_000)
    bridge._offline_interval = 3000
    assert wait_until(qapp, lambda: bridge.state == states.OFFLINE)
    bridge._timer.setInterval(3000)
    bridge._timer.start()
    first = state_text(window)
    assert wait_until(qapp, lambda: state_text(window) != first, 3)


def test_retry_now_does_not_wait_for_the_timer(env, qapp, fake):
    fake.go_down()
    window, bridge, store = env.open(interval=60_000)
    assert wait_until(qapp, lambda: bridge.state == states.OFFLINE)
    fake.come_back()
    retry_button(window).click()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED, 5)
    assert retry_button(window).isHidden() and state_text(window) == "● connected"


def test_the_refresh_is_more_frequent_while_offline(env, qapp, fake):
    fake.go_down()
    window, bridge, _ = env.open(interval=60_000)
    assert wait_until(qapp, lambda: bridge.state == states.OFFLINE)
    assert bridge._timer.interval() == bridge._offline_interval < 60_000
    fake.come_back()
    bridge.refresh()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    assert bridge._timer.interval() == 60_000


def test_the_backend_coming_back_is_noticed_by_itself(env, qapp, fake):
    fake.go_down()
    window, bridge, store = env.open(interval=60_000)
    bridge._offline_interval = 150
    assert wait_until(qapp, lambda: bridge.state == states.OFFLINE)
    bridge._timer.setInterval(150)
    bridge._timer.start()
    fake.come_back()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED, 5)


def test_reconnected_is_signalled_only_after_a_problem(env, qapp, fake):
    heard = []
    window, bridge, _ = env.open(start=False)
    bridge.reconnected.connect(lambda: heard.append(1))
    bridge.start()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    assert heard == []  # the first connection is not a reconnection
    fake.go_down()
    bridge.refresh()
    assert wait_until(qapp, lambda: bridge.state == states.OFFLINE)
    fake.come_back()
    bridge.refresh()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    assert heard == [1]
    bridge.refresh()
    time.sleep(0.2)
    qapp.processEvents()
    assert heard == [1]


def test_a_rejected_token_shows_the_retry_button_too(env, qapp, fake):
    fake.tokens.discard("tok-a")
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.UNAUTHORIZED)
    assert "token rejected" in state_text(window) and not retry_button(window).isHidden()
    fake.tokens.add("tok-a")
    retry_button(window).click()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)


# --- conversations recover by themselves ----------------------------------------------------------------------------------------------------

def test_a_conversation_that_could_not_be_loaded_loads_when_the_backend_is_back(env, qapp, fake):
    session = fake.add_session()
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "an old question"}]})
    window, bridge, store = env.open(interval=60_000)
    assert wait_until(qapp, lambda: session.id in window.sidebar.ids())  # the sidebar knows it from the first sync
    fake.go_down()
    bridge.refresh()
    assert wait_until(qapp, lambda: bridge.state == states.OFFLINE)
    window.sidebar.select(session.id)
    pane = window.pane
    assert wait_until(qapp, lambda: ss.NOTICE_LOAD in pane.notice_kinds())
    assert pane.status_text == "Could not load"
    fake.come_back()
    bridge.refresh()  # the periodic refresh would do this
    assert wait_until(qapp, lambda: pane.chat.state.loaded and pane.notice_kinds() == [], 8)
    assert texts(pane) == [("UserBlock", "an old question")]


def test_a_refused_conversation_recovers_when_the_token_works_again(env, qapp, fake):
    session = fake.add_session()
    window, bridge, store = env.open(interval=60_000)
    assert wait_until(qapp, lambda: session.id in window.sidebar.ids())
    fake.tokens.discard("tok-a")
    window.sidebar.select(session.id)
    pane = window.pane
    assert wait_until(qapp, lambda: ss.NOTICE_UNAVAILABLE in pane.notice_kinds())
    assert not pane.composer.edit.isEnabled()
    assert pane.notice_frame(ss.NOTICE_UNAVAILABLE).findChild(QPushButton, "recoverButton") is not None
    fake.tokens.add("tok-a")
    pane.notice_frame(ss.NOTICE_UNAVAILABLE).findChild(QPushButton, "recoverButton").click()
    assert wait_until(qapp, lambda: ss.NOTICE_UNAVAILABLE not in pane.notice_kinds() and pane.composer.edit.isEnabled(), 8)


def test_an_answer_that_was_interrupted_by_a_restart_is_whole_in_the_end(env, qapp, fake):
    text = "".join(f"w{i} " for i in range(30))
    fake.replies.append(Reply(text=text, chunks=30, delay=0.06, start_delay=0.1))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "write a lot")
    assert wait_until(qapp, lambda: len(blocks(pane)) == 2 and len(blocks(pane)[1].body_text()) > 10)
    fake.go_down()
    assert wait_until(qapp, lambda: ss.NOTICE_CONNECTION in pane.notice_kinds(), 8)
    assert "Connection to the backend lost" in pane.notice_text(ss.NOTICE_CONNECTION)
    assert pane.notice_frame(ss.NOTICE_CONNECTION).findChild(QPushButton, "retryNowButton") is not None
    time.sleep(0.4)
    fake.come_back()
    pane.notice_frame(ss.NOTICE_CONNECTION).findChild(QPushButton, "retryNowButton").click()
    assert wait_until(qapp, lambda: settled(pane, fake, session) and ss.NOTICE_CONNECTION not in pane.notice_kinds(), 20)
    assert texts(pane)[-1] == ("AssistantBlock", text.strip())


# --- messages that did not get through -------------------------------------------------------------------------------------------------------

def test_a_message_that_did_not_arrive_can_be_sent_again(env, qapp, fake):
    fake.replies.append(Reply(text="here is the answer"))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    pane.chat.controller._verify_grace = 0.05
    fake.go_down()
    type_and_send(pane, "are you there?")
    assert wait_until(qapp, lambda: len(blocks(pane)) == 1 and "not confirmed" in blocks(pane)[0].state_text)
    assert blocks(pane)[0]._actions.isHidden()  # not known yet: sending it again could make it arrive twice
    fake.come_back()
    assert wait_until(qapp, lambda: len(blocks(pane)) == 1 and "not sent" in blocks(pane)[0].state_text, 10)
    block = blocks(pane)[0]
    assert "the backend did not receive it" in block.state_text and not block._actions.isHidden()
    block.findChild(QPushButton, "retryMessageButton").click()
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    assert texts(pane) == [("UserBlock", "are you there?"), ("AssistantBlock", "here is the answer")]
    assert all(getattr(b, "state_text", "") == "" for b in blocks(pane))  # the old attempt is gone, not duplicated
    assert [m["content"][0]["text"] for m in session.messages if m["role"] == "user"] == ["are you there?"]


def test_a_refused_message_can_be_removed(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    fake.fail_next("POST", "/messages$", status=400, code="validation_error", message="nope")
    type_and_send(pane, "this one is refused")
    assert wait_until(qapp, lambda: len(blocks(pane)) == 1 and "not sent" in blocks(pane)[0].state_text)
    assert not blocks(pane)[0]._actions.isHidden()
    blocks(pane)[0].findChild(QPushButton, "discardMessageButton").click()
    assert wait_until(qapp, lambda: blocks(pane) == [])


def test_a_message_that_did_arrive_is_not_offered_for_sending_again(env, qapp, fake):
    fake.replies.append(Reply(text="yes"))
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    pane.chat.controller._verify_grace = 0.05
    original = pane.chat.controller._client.submit

    def lose_the_answer(session_id, messages, **kw):
        original(session_id, messages, **kw)
        from etcode_desktop.core.errors import Unreachable
        raise Unreachable("the answer was lost")

    pane.chat.controller._client.submit = lose_the_answer
    type_and_send(pane, "did you get this?")
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    assert texts(pane) == [("UserBlock", "did you get this?"), ("AssistantBlock", "yes")]
    assert all(b._actions.isHidden() for b in blocks(pane) if type(b).__name__ == "UserBlock")


def test_sending_again_is_refused_when_the_session_cannot_be_used(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    fake.fail_next("POST", "/messages$", status=400, code="validation_error", message="nope")
    type_and_send(pane, "refused")
    assert wait_until(qapp, lambda: len(blocks(pane)) == 1 and "not sent" in blocks(pane)[0].state_text)
    key = pane.chat.state.model.entries()[0].key
    pane.chat.state.apply(c.Unavailable("gone", True))
    before = len(fake.requests)
    pane._resend(key)
    time.sleep(0.2)
    assert len(fake.requests) == before and len(blocks(pane)) == 1


# --- shutting down ------------------------------------------------------------------------------------------------------------------------------

def test_shutdown_ends_the_conversations_and_can_be_called_twice(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    chat = pane.chat
    window.shutdown()
    window.shutdown()
    assert chat.controller._closed.is_set() and pane.chat is None


def run_app(*args, **extra):
    environ = {k: v for k, v in os.environ.items() if not k.startswith("ETCODE_")}
    environ.update(PYTHONPATH=str(ROOT), **extra)
    return subprocess.Popen([sys.executable, "-m", "etcode_desktop", *args], cwd=ROOT, env=environ,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.parametrize("signal_number", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_quits_the_app_and_ends_the_commands_of_the_model(tmp_path, signal_number):
    from fake_backend import FakeBackend
    home = tmp_path / "home"
    home.mkdir()
    pid_file = tmp_path / "pid"
    with FakeBackend(tokens=("tok-main",)) as backend:
        session = backend.add_session()
        session.status = "awaiting_tools"
        session.messages.append({"role": "user", "content": [{"type": "text", "text": "run"}]})
        calls = [{"id": "c1", "name": "bash", "arguments": {"command": f"sleep 120 & echo $! > {pid_file}; sleep 120"}}]
        session.messages.append({"role": "assistant", "text": {"type": "text", "text": "ok"}, "reasoning": {"type": "text", "text": ""},
                                 "tool_calls": calls, "status": "complete", "error": None})
        session.pending = [{"id": c["id"], "name": c["name"], "arguments": c["arguments"]} for c in calls]
        process = run_app("--no-single-instance", ETCODE_BACKEND_URL=backend.url, ETCODE_API_TOKEN="tok-main",
                          ETCODE_PROVISIONED_FILE=str(tmp_path / "none.json"), XDG_CONFIG_HOME=str(tmp_path / "config"),
                          XDG_DATA_HOME=str(tmp_path / "data"), XDG_STATE_HOME=str(tmp_path / "state"),
                          XDG_RUNTIME_DIR=str(tmp_path / "run"), HOME=str(home), QT_QPA_PLATFORM="offscreen")
        try:
            deadline = time.monotonic() + 20
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert pid_file.exists(), "the command did not start"
            child = int(pid_file.read_text())
            started = time.monotonic()
            process.send_signal(signal_number)
            assert process.wait(timeout=15) is not None
            assert time.monotonic() - started < 10, "the app did not quit quickly"
            time.sleep(0.3)
            assert not os.path.exists(f"/proc/{child}") or "Z (zombie)" in open(f"/proc/{child}/status").read(), "the command was left running"
        finally:
            if process.poll() is None:
                process.kill()
