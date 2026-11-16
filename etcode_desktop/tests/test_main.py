"""Run the real entry point in a subprocess: it must start, show a window and exit cleanly."""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]

# Start the app, quit it from a timer once the event loop runs, and report what the window looked like.
DRIVER = """
import os
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import etcode_desktop.__main__ as entry

def quit_soon():
    app = QApplication.instance()
    windows = [w for w in app.topLevelWidgets() if w.isVisible()]
    main_windows = [w for w in windows if hasattr(w, "sidebar")]
    print("visible windows:", len(windows), "| title:", main_windows[0].windowTitle() if main_windows else None)
    print("titles:", sorted(w.windowTitle() for w in windows))
    sidebars = [w.sidebar.ids() for w in windows if hasattr(w, "sidebar")]
    print("sessions:", sidebars[0] if sidebars else None)
    app.quit()

def open_first_session_then_quit():
    app = QApplication.instance()
    window = next(w for w in app.topLevelWidgets() if hasattr(w, "sidebar"))
    ids = window.sidebar.ids()
    if ids:
        window.sidebar.select(ids[0])
    def report():
        window.pane.transcript.flush()
        print("transcript:", [b.body_text() for b in window.pane.transcript.blocks_in_order()])
        print("status:", window.pane.status_text)
        quit_soon()
    QTimer.singleShot(int(os.environ.get("DRIVER_OPEN_MS", "1500")), report)

original_exec = QApplication.exec
def exec_with_timer(*args):
    if os.environ.get("DRIVER_OPEN_FIRST"):
        QTimer.singleShot(int(os.environ.get("DRIVER_WAIT_MS", "300")), open_first_session_then_quit)
        return original_exec()
    QTimer.singleShot(int(os.environ.get("DRIVER_WAIT_MS", "300")), quit_soon)
    return original_exec()
QApplication.exec = exec_with_timer

sys.exit(entry.main(sys.argv[1:]))
"""


def run_app(env, *args):
    environ = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-c", DRIVER, *args], cwd=ROOT, env=environ,
                          capture_output=True, text=True, timeout=60)


def test_app_starts_and_exits(env, monkeypatch):
    monkeypatch.setenv("ETCODE_API_TOKEN", "tok-some-token")
    result = run_app(env)
    assert result.returncode == 0, result.stderr
    assert "visible windows: 1 | title: etcode" in result.stdout


def test_without_a_token_the_connection_settings_open_at_once(env):
    result = run_app(env, "--no-single-instance")
    assert result.returncode == 0, result.stderr
    assert "titles: ['Connection settings', 'etcode']" in result.stdout, result.stdout


def test_the_session_store_is_created_privately(env):
    result = run_app(env, "--no-single-instance")
    assert result.returncode == 0, result.stderr
    store = env / "data" / "etcode_desktop" / "store.sqlite3"
    assert store.is_file() and stat.S_IMODE(store.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.parent.stat().st_mode) == 0o700


def test_the_real_app_lists_the_sessions_of_a_backend_and_remembers_them(env, monkeypatch):
    pytest.importorskip("flask", reason="the fake backend needs Flask")
    from fake_backend import FakeBackend
    with FakeBackend(tokens=("tok-main",)) as backend:
        first, second = backend.add_session(), backend.add_session()
        monkeypatch.setenv("ETCODE_BACKEND_URL", backend.url)
        monkeypatch.setenv("ETCODE_API_TOKEN", "tok-main")
        monkeypatch.setenv("DRIVER_WAIT_MS", "2500")
        result = run_app(env, "--no-single-instance")
        assert result.returncode == 0, result.stderr
        listed = [line for line in result.stdout.splitlines() if line.startswith("sessions:")][0]
        assert first.id in listed and second.id in listed
    # the backend is gone now, but the sessions are still shown, from the store
    monkeypatch.setenv("DRIVER_WAIT_MS", "500")
    again = run_app(env, "--no-single-instance")
    assert again.returncode == 0, again.stderr
    listed = [line for line in again.stdout.splitlines() if line.startswith("sessions:")][0]
    assert first.id in listed and second.id in listed
    log_text = (env / "state" / "etcode_desktop" / "desktop.log").read_text()
    assert "tok-main" not in log_text + result.stderr + again.stderr


def test_the_real_app_shows_the_conversation_of_a_session(env, monkeypatch):
    pytest.importorskip("flask", reason="the fake backend needs Flask")
    from fake_backend import FakeBackend
    with FakeBackend(tokens=("tok-main",)) as backend:
        session = backend.add_session()
        session.messages += [
            {"role": "user", "content": [{"type": "text", "text": "What is 6 times 7?"}]},
            {"role": "assistant", "text": {"type": "text", "text": "It is **42**."}, "reasoning": {"type": "text", "text": ""},
             "tool_calls": [], "status": "complete", "error": None},
        ]
        monkeypatch.setenv("ETCODE_BACKEND_URL", backend.url)
        monkeypatch.setenv("ETCODE_API_TOKEN", "tok-main")
        monkeypatch.setenv("DRIVER_OPEN_FIRST", "1")
        monkeypatch.setenv("DRIVER_WAIT_MS", "2000")
        monkeypatch.setenv("DRIVER_OPEN_MS", "2000")
        result = run_app(env, "--no-single-instance")
    assert result.returncode == 0, result.stderr
    assert "transcript: ['What is 6 times 7?', 'It is 42.']" in result.stdout, result.stdout
    assert "status: idle" in result.stdout


def test_the_real_app_runs_the_tools_that_a_session_was_waiting_for(env, monkeypatch):
    pytest.importorskip("flask", reason="the fake backend needs Flask")
    from fake_backend import FakeBackend, Reply
    from etcode_desktop.core.store import JOURNAL_POSTED, Scope, SessionStore
    (env / "home").mkdir(exist_ok=True)
    with FakeBackend(tokens=("tok-main",)) as backend:
        backend.auto_reply = True
        backend.replies.append(Reply(text="got it"))
        session = backend.add_session()
        session.status = "awaiting_tools"
        session.messages.append({"role": "user", "content": [{"type": "text", "text": "run something"}]})
        calls = [{"id": "c1", "name": "bash", "arguments": {"command": "echo from-the-app"}},
                 {"id": "c2", "name": "write_to_file", "arguments": {"path": "out.txt", "content": "written by the app\n"}}]
        session.messages.append({"role": "assistant", "text": {"type": "text", "text": "ok"}, "reasoning": {"type": "text", "text": ""},
                                 "tool_calls": calls, "status": "complete", "error": None})
        session.pending = [{"id": c["id"], "name": c["name"], "arguments": c["arguments"]} for c in calls]
        monkeypatch.setenv("ETCODE_BACKEND_URL", backend.url)
        monkeypatch.setenv("ETCODE_API_TOKEN", "tok-main")
        monkeypatch.setenv("DRIVER_WAIT_MS", "3500")  # nobody opens the session: the app picks it up by itself
        result = run_app(env, "--no-single-instance")
        assert result.returncode == 0, result.stderr
        results = {m["tool_call_id"]: m["content"][0]["text"] for m in session.messages if m["role"] == "tool"}
        assert results["c1"] == "<returncode>0</returncode>\n<stderr></stderr>\n<stdout>from-the-app\n</stdout>"
        assert results["c2"] == "Wrote 19 characters to out.txt"
        assert session.messages[-1]["text"]["text"] == "got it" and session.status == "idle"
    assert (env / "home" / "out.txt").read_text() == "written by the app\n"  # relative paths are relative to home
    store = SessionStore(env / "data" / "etcode_desktop" / "store.sqlite3")
    scope = Scope.for_token(backend.url, "tok-main")
    assert [e.state for e in store.journal_entries(scope, session.id)] == [JOURNAL_POSTED, JOURNAL_POSTED]
    store.close()


def test_log_file_is_written_without_the_token(env):
    os.environ["ETCODE_API_TOKEN"] = "tok-NEVER-LOGGED-1"
    try:
        result = run_app(env, "--no-single-instance")
    finally:
        del os.environ["ETCODE_API_TOKEN"]
    assert result.returncode == 0, result.stderr
    log_text = (env / "state" / "etcode_desktop" / "desktop.log").read_text()
    assert "starting etcode-desktop" in log_text
    assert "tok-NEVER-LOGGED-1" not in log_text + result.stderr + result.stdout


def test_second_launch_exits_immediately_while_the_first_runs(env):
    environ = dict(os.environ, PYTHONPATH=str(ROOT))
    first = subprocess.Popen([sys.executable, "-m", "etcode_desktop"], cwd=ROOT, env=environ,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # wait until the first instance has taken the lock
        import time
        lock = env / "run" / "etcode_desktop.lock"
        deadline = time.monotonic() + 20
        while not lock.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert lock.exists(), "first instance did not start"
        time.sleep(0.5)
        # the real `python -m` entry point must reach the log file (its module name is __main__)
        assert "starting etcode-desktop" in (env / "state" / "etcode_desktop" / "desktop.log").read_text()
        second = subprocess.run([sys.executable, "-m", "etcode_desktop"], cwd=ROOT, env=environ,
                                capture_output=True, text=True, timeout=30)
        assert second.returncode == 0, second.stderr
        assert first.poll() is None, "the first instance must keep running"
    finally:
        first.terminate()
        first.wait(timeout=10)
