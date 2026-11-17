"""The session controller against the REAL backend and worker (fake LLM upstream).

test_controller.py uses the scriptable fake backend. These tests make sure that the controller also
works with what the real code does: event order, ids, queueing, cancelling, tool calls.
"""
import threading
import time

import pytest

from real_backend import RealBackend, Upstream  # noqa: E402  (skips this module if the backend is not available)

from etcode_desktop.core import controller as c  # noqa: E402
from etcode_desktop.core.session_state import NOTICE_TOOLS, SessionState  # noqa: E402
from etcode_desktop.core.sse import Backoff  # noqa: E402
from etcode_desktop.core.store import Scope, SessionStore  # noqa: E402


class RealHarness:
    def __init__(self, real, store, make_client, model, tools=None, **session_args):
        self.real, self.store = real, store
        self.client = make_client(real)
        self.scope = Scope.for_token(real.url, "tok-a")
        self.session = self.client.create_session(model, **session_args)
        store.upsert_from_meta(self.scope, self.session)
        self.state = SessionState()
        self.events = []
        self._lock = threading.RLock()
        self.controller = c.SessionController(
            self.client, store, self.scope, self.session.id, self._on_event,
            backoff=Backoff(initial=0.01, maximum=0.05, give_up_after=10.0), retry_delays=(0.05,), poll_delays=(0.02, 0.05), tools=tools)

    def _on_event(self, event):
        with self._lock:
            self.events.append(event)
            self.state.apply(event)

    def of(self, kind):
        with self._lock:
            return [e for e in self.events if isinstance(e, kind)]

    def wait(self, condition, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if condition():
                    return True
            time.sleep(0.01)
        with self._lock:
            return bool(condition())

    def settled(self):
        meta = self.client.get_session(self.session.id)
        with self._lock:
            return (meta.status in ("idle", "failed") and not self.controller.is_following
                    and set(range(meta.message_count)) <= self.state.model.seen_indexes
                    and not any(getattr(e, "state", "") == "sending" for e in self.state.model.entries()))

    def wait_settled(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.settled():
                return
            time.sleep(0.02)
        raise AssertionError(f"not settled: {self.client.get_session(self.session.id).status}, "
                             f"{[type(e).__name__ for e in self.events]}")

    def send(self, text, **kw):
        key = self.state.begin_send(text)
        self.controller.send(key, text, **kw)
        return key

    def shown(self):
        with self._lock:
            return [(e.role, e.index, e.text) for e in self.state.model.entries()]

    def backend_view(self):
        """What the backend's history says, in the same shape as shown() (system and tool messages left out)."""
        view = []
        for entry in self.client.messages(self.session.id):
            message = entry.message
            if message["role"] == "user":
                view.append(("user", entry.index, "\n".join(p["text"] for p in message["content"] if p["type"] == "text")))
            elif message["role"] == "assistant":
                view.append(("assistant", entry.index, message["text"]["text"]))
        return view


@pytest.fixture
def real(tmp_path):
    backend = RealBackend(tmp_path / "backend")
    yield backend
    backend.stop()


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "store.sqlite3")
    yield s
    s.close()


@pytest.fixture
def harness(real, store, make_client):
    created = []
    model = next(m.tech_name for m in make_client(real).models() if m.available)

    def make(tools=None, **session_args):
        h = RealHarness(real, store, make_client, model, tools=tools, **session_args)
        created.append(h)
        return h

    yield make
    for h in created:
        h.controller.close()


def test_a_reply_is_streamed_and_ends_up_equal_to_the_backends_history(real, harness):
    real.upstream.script = [Upstream.text(["Hel", "lo ", "world"], delay=0.02)]
    h = harness()
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    h.send("say hello")
    h.wait_settled()
    assert h.shown() == [("user", 1, "say hello"), ("assistant", 2, "Hello world")]
    assert [(r, i) for r, i, _ in h.shown()] == [(r, i) for r, i, _ in h.backend_view()]
    assert h.shown()[-1][2] == "Hello world"
    assert h.state.cost_total == "0.0042" and h.state.cost_text() == "$0.0042"
    assert h.store.get(h.scope, h.session.id).title == "say hello"
    assert h.state.status == "idle" and not h.state.notices


def test_a_session_that_is_already_streaming_is_joined_without_duplicates(real, harness, make_client):
    real.upstream.script = [Upstream.text([f"w{i} " for i in range(15)], delay=0.06)]
    first = harness()
    first.send("long answer please")
    assert first.wait(lambda: first.of(c.LiveText))
    # a second controller (the window was reopened) joins in the middle of the generation
    second = RealHarness.__new__(RealHarness)
    second.real, second.store, second.client, second.scope, second.session = first.real, first.store, make_client(real), first.scope, first.session
    second.state, second.events, second._lock = SessionState(), [], threading.RLock()
    second.controller = c.SessionController(second.client, first.store, first.scope, first.session.id, second._on_event,
                                            backoff=Backoff(initial=0.01, maximum=0.05, give_up_after=10.0))
    second.controller.open()
    second.wait_settled()
    first.wait_settled()
    expected = "".join(f"w{i} " for i in range(15))
    for h in (first, second):
        assert h.shown()[-1] == ("assistant", 2, expected)
        assert len(h.state.model.entries()) == 2, h.shown()  # no duplicate of the streamed message
    second.controller.close()


def test_a_message_sent_during_a_reply_is_queued_and_answered_next(real, harness):
    real.upstream.script = [Upstream.text(["one ", "two ", "three"], delay=0.15), Upstream.text("second answer")]
    h = harness()
    h.send("first")
    assert h.wait(lambda: h.state.busy)
    second = h.send("second")
    assert h.wait(lambda: any(e.local_key == second for e in h.of(c.UserSent)))
    assert [e for e in h.of(c.UserSent) if e.local_key == second][0].queued is True
    h.wait_settled()
    assert [(r, t) for r, _, t in h.shown()] == [("user", "first"), ("assistant", "one two three"),
                                                  ("user", "second"), ("assistant", "second answer")]
    assert h.shown() == h.backend_view()


def test_stop_cancels_the_reply_and_keeps_what_was_written(real, harness):
    real.upstream.script = [Upstream.text(["x"] * 60, delay=0.1)]
    h = harness()
    h.send("go")
    assert h.wait(lambda: h.of(c.LiveText))
    h.controller.stop()
    h.wait_settled()
    last = h.state.model.entries()[-1]
    assert last.status == "cancelled" and last.text.startswith("x")
    assert h.of(c.GenerationCancelled)
    assert h.shown() == h.backend_view()


def test_a_failing_provider_is_shown_as_an_error_message(real, harness):
    real.upstream.script = [Upstream.text("fine"), lambda handler: (handler.send_response(400), handler.send_header("Content-Length", "2"),
                                                                    handler.end_headers(), handler.wfile.write(b"{}"))]
    h = harness()
    h.send("first")
    h.wait_settled()
    h.send("second")
    h.wait_settled()
    last = h.state.model.entries()[-1]
    assert last.role == "assistant" and last.status == "error" and last.error and last.error["code"]
    assert h.state.status == "failed"
    h.send("third")  # the session accepts the next message
    h.wait_settled()
    assert h.state.model.entries()[-1].text == "default reply"


def test_tool_calls_wait_and_stop_cancels_them(real, harness):
    real.upstream.script = [Upstream.tools([("call_a", "bash", {"command": "pwd"}), ("call_b", "read_file", {"path": "x"})])]
    h = harness()
    h.send("run things")
    assert h.wait(lambda: NOTICE_TOOLS in h.state.notices)
    assert h.wait(lambda: not h.controller.is_following)
    assistant = h.state.model.entries()[-1]
    assert [(t.id, t.name) for t in assistant.tool_calls] == [("call_a", "bash"), ("call_b", "read_file")]
    assert [p.id for p in h.of(c.ToolsPending)[0].calls] == ["call_a", "call_b"]
    assert h.state.status == "awaiting_tools"
    h.controller.stop()
    h.wait_settled()
    assert all("cancelled" in t.result[0]["text"].lower() for t in h.state.model.entries()[-1].tool_calls)
    assert NOTICE_TOOLS not in h.state.notices


def test_tool_calls_survive_reopening_the_session(real, harness, make_client):
    real.upstream.script = [Upstream.tools([("call_a", "bash", {"command": "pwd"})])]
    h = harness()
    h.send("run it")
    assert h.wait(lambda: NOTICE_TOOLS in h.state.notices)
    h.controller.close()
    again = RealHarness.__new__(RealHarness)
    again.real, again.store, again.client, again.scope, again.session = real, h.store, make_client(real), h.scope, h.session
    again.state, again.events, again._lock = SessionState(), [], threading.RLock()
    again.controller = c.SessionController(again.client, h.store, h.scope, h.session.id, again._on_event)
    again.controller.open()
    assert again.wait(lambda: NOTICE_TOOLS in again.state.notices)
    assert again.state.model.entries()[-1].tool_calls[0].name == "bash"
    again.controller.close()


def test_a_backend_web_search_runs_through_the_client_in_order(real, harness, monkeypatch):
    class Search:
        def search(self, query):
            return [{"title": "Python", "url": "https://python.org", "description": "A language", "age": None}]
    monkeypatch.setattr("etcode_backend.backend_tools.get_search_provider", lambda settings: Search())
    real.upstream.script = [Upstream.tools([("search_1", "websearch", {"query": "python"})]), Upstream.text("Python is a language.")]
    h = harness(tools=ToolRunner(ToolPool(2)), websearch=True)
    h.send("what is python")
    h.wait_settled()
    entries = h.state.model.entries()
    assert [e.role for e in entries] == ["user", "assistant", "assistant"]
    result = entries[1].tool_calls[0].result
    assert result and "Python" in result[0]["text"]
    assert entries[2].text == "Python is a language." and NOTICE_TOOLS not in h.state.notices
    assert h.shown() == h.backend_view()


def test_the_history_of_a_finished_session_is_loaded_in_full(real, harness):
    real.upstream.script = [Upstream.text("answer one"), Upstream.text("answer two")]
    h = harness()
    h.send("q1")
    h.wait_settled()
    h.send("q2")
    h.wait_settled()
    h.controller.close()
    fresh = SessionState()
    entries = h.client.messages(h.session.id)
    from etcode_desktop.core.models import SessionMeta
    fresh.apply(c.HistoryLoaded(tuple(entries), SessionMeta(id=h.session.id, model="m", status="idle")))
    assert [(e.role, e.text) for e in fresh.model.entries()] == [
        ("user", "q1"), ("assistant", "answer one"), ("user", "q2"), ("assistant", "answer two")]


# --- running tools against the real backend ---------------------------------------------------------------------------------------

import os  # noqa: E402

from etcode_desktop.core.store import JOURNAL_POSTED, JOURNAL_RUNNING  # noqa: E402
from etcode_desktop.core.tools.scheduler import ToolPool, ToolRunner  # noqa: E402

from fake_backend import PNG  # noqa: E402


def tool_message_texts(h):
    return {e.message["tool_call_id"]: e.message["content"][0]["text"] for e in h.client.messages(h.session.id) if e.message["role"] == "tool"}


def runner(tmp_path):
    return ToolRunner(ToolPool(8), home=tmp_path)


def test_a_command_runs_and_the_model_gets_exactly_its_result(real, harness, tmp_path):
    real.upstream.script = [Upstream.tools([("call_a", "bash", {"command": "echo hello; echo oops >&2; exit 2", "directory": str(tmp_path)})]),
                            Upstream.text("The command printed hello.")]
    h = harness(tools=runner(tmp_path))
    h.send("run it")
    h.wait_settled()
    expected = "<returncode>2</returncode>\n<stderr>oops\n</stderr>\n<stdout>hello\n</stdout>"
    assert tool_message_texts(h) == {"call_a": expected}
    # and that is what the language model was sent in the next request
    second = real.upstream.requests[1]["messages"]
    tool_turn = next(m for m in second if m.get("role") == "tool")
    sent = tool_turn["content"] if isinstance(tool_turn["content"], str) else "".join(part.get("text", "") for part in tool_turn["content"])
    assert tool_turn["tool_call_id"] == "call_a" and sent == expected
    assert h.shown()[-1][2] == "The command printed hello."
    assert h.store.journal_get(h.scope, h.session.id, "call_a").state == JOURNAL_POSTED
    assert NOTICE_TOOLS not in h.state.notices


import json  # noqa: E402


def test_calls_run_in_model_order_with_the_real_backend(real, harness, tmp_path):
    path = tmp_path / "data.txt"
    real.upstream.script = [Upstream.tools([
        ("w", "write_to_file", {"path": str(path), "content": "alpha\n"}),
        ("r", "read_file", {"path": str(path)}),
        ("s1", "bash", {"command": "sleep 0.7; echo one", "directory": str(tmp_path)}),
        ("s2", "bash", {"command": "sleep 0.7; echo two", "directory": str(tmp_path)}),
        ("s3", "bash", {"command": "sleep 0.7; echo three", "directory": str(tmp_path)})]), Upstream.text("done")]
    h = harness(tools=runner(tmp_path))
    started = time.monotonic()
    h.send("go")
    h.wait_settled(20)
    assert time.monotonic() - started >= 2.1, "the three sleeps ran side by side instead of one after another"
    results = tool_message_texts(h)
    assert "<content>alpha\n</content>" in results["r"] and results["w"] == f"Wrote 6 characters to {path}"
    assert [e.message["tool_call_id"] for e in h.client.messages(h.session.id) if e.message["role"] == "tool"] == ["w", "r", "s1", "s2", "s3"]


def test_stop_ends_a_command_with_the_real_backend(real, harness, tmp_path):
    pid_file = tmp_path / "pid"
    real.upstream.script = [Upstream.tools([("call_a", "bash", {"command": f"sleep 60 & echo $! > {pid_file}; sleep 60", "directory": str(tmp_path)})])]
    h = harness(tools=runner(tmp_path))
    h.send("run long")
    assert h.wait(lambda: pid_file.exists())
    started = time.monotonic()
    h.controller.stop()
    h.wait_settled(20)
    assert time.monotonic() - started < 8
    assert "cancelled" in tool_message_texts(h)["call_a"].lower()
    time.sleep(0.3)
    pid = int(pid_file.read_text())
    assert not os.path.exists(f"/proc/{pid}") or "Z (zombie)" in open(f"/proc/{pid}/status").read()


def test_a_call_that_was_running_when_the_app_died_is_answered_with_a_note(real, harness, make_client, tmp_path):
    real.upstream.script = [Upstream.tools([("call_a", "bash", {"command": "echo must-not-run > " + str(tmp_path / "ran.txt"), "directory": str(tmp_path)})]),
                            Upstream.text("I will check the state.")]
    first = harness()  # no runner: the call just waits
    first.send("do it")
    assert first.wait(lambda: NOTICE_TOOLS in first.state.notices)
    first.controller.close()
    # the previous run died while the command ran
    first.store.journal_start(first.scope, first.session.id, "call_a", "bash", {"command": "..."})
    assert first.store.journal_get(first.scope, first.session.id, "call_a").state == JOURNAL_RUNNING
    again = RealHarness.__new__(RealHarness)
    again.real, again.store, again.client, again.scope, again.session = real, first.store, make_client(real), first.scope, first.session
    again.state, again.events, again._lock = SessionState(), [], threading.RLock()
    again.controller = c.SessionController(again.client, first.store, first.scope, first.session.id, again._on_event,
                                           backoff=Backoff(initial=0.01, maximum=0.05, give_up_after=10.0), tools=runner(tmp_path))
    again.controller.open()
    again.wait_settled(15)
    assert not (tmp_path / "ran.txt").exists()  # it was not run again
    assert tool_message_texts(again)["call_a"].startswith("Error: the desktop client was interrupted while executing this tool call")
    assert again.shown()[-1][2] == "I will check the state."
    again.controller.close()


def test_an_image_is_seen_by_the_model_through_the_real_backend(real, harness, make_client, tmp_path):
    client = make_client(real)
    model = next(m for m in client.models() if m.available)
    if not model.supports_images:
        pytest.skip("the first available model has no image support")
    (tmp_path / "shot.png").write_bytes(PNG)
    real.upstream.script = [Upstream.tools([("call_a", "view_image", {"path": "shot.png"})]), Upstream.text("A tiny picture.")]
    h = harness(tools=runner(tmp_path))
    h.send("look at shot.png")
    h.wait_settled(15)
    content = next(e.message for e in h.client.messages(h.session.id) if e.message["role"] == "tool")["content"]
    assert content[0]["type"] == "image" and content[1]["text"].startswith("Image shot.png (image/png")
    second = json.dumps(real.upstream.requests[1]["messages"])
    assert "image_url" in second and "data:image/png;base64" in second  # the backend passed the picture on
    assert h.shown()[-1][2] == "A tiny picture."


# --- the API process restarts -------------------------------------------------------------------------------------------------------

def test_an_answer_survives_a_restart_of_the_api_process(real, harness):
    parts = [f"p{i} " for i in range(25)]
    real.upstream.script = [Upstream.text(parts, delay=0.08)]
    h = harness()
    h.send("write a lot")
    assert h.wait(lambda: len(h.of(c.LiveText)) >= 3)
    real.go_down()
    assert h.wait(lambda: h.of(c.ConnectionLost), 10)
    time.sleep(0.5)  # the worker keeps writing meanwhile
    real.come_back()
    h.controller.retry_now()
    h.wait_settled(30)
    assert h.shown()[-1] == ("assistant", 2, "".join(parts))
    assert len(h.state.model.entries()) == 2 and h.shown() == h.backend_view()


def test_a_message_sent_while_the_api_is_down_is_resolved_with_the_real_history(real, harness):
    real.upstream.script = [Upstream.text("fine")]
    h = harness()
    h.controller._verify_grace = 0.05
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    real.go_down()
    key = h.send("hello?")
    assert h.wait(lambda: h.state.model.get(key) is not None and h.state.model.get(key).state == "unknown", 10)
    real.come_back()
    assert h.wait(lambda: h.state.model.get(key).state == "failed", 15)
    assert h.client.messages(h.session.id)[-1].message["role"] == "system"  # really not there


# --- pictures, models and options against the real backend --------------------------------------------------------------------------

from etcode_desktop.core.attachments import Attachment  # noqa: E402


def png(width=40, height=30):
    from PySide6.QtGui import QColor, QImage
    from etcode_desktop.ui.composer import png_bytes
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#336699"))
    return png_bytes(image)


def test_attached_pictures_reach_the_model_through_the_real_backend(real, harness, make_client):
    if not next(m for m in make_client(real).models() if m.available).supports_images:
        pytest.skip("the first available model has no image support")
    pytest.importorskip("PySide6.QtGui")
    real.upstream.script = [Upstream.text("a blue picture")]
    h = harness()
    attachments = [Attachment("blue.png", png(), "image/png")]
    key = h.state.begin_send("what is this?", (), attachments)
    h.controller.send(key, "what is this?", (), attachments)
    h.wait_settled()
    sent = json.dumps(real.upstream.requests[0]["messages"])
    assert "image_url" in sent and "data:image/png;base64" in sent and "what is this?" in sent
    assert h.shown() == h.backend_view() and h.state.model.entries()[0].image_hashes
    assert len(h.state.model.entries()) == 2  # the pending message was replaced by its committed copy


def test_the_options_of_a_message_reach_the_provider(real, harness):
    real.upstream.script = [Upstream.text("ok")]
    h = harness()
    key = h.state.begin_send("hello")
    h.controller.send(key, "hello", temperature=0.4, max_tokens=123, reasoning_effort="low")
    h.wait_settled()
    request = real.upstream.requests[0]
    assert request["temperature"] == 0.4 and request["max_tokens"] == 123 and "low" in json.dumps(request)


def test_the_model_can_be_changed_between_models_that_both_take_pictures(real, harness, make_client):
    models = [m for m in make_client(real).models() if m.available and m.supports_images]
    if len(models) < 2:
        pytest.skip("the backend needs two models that take pictures")
    h = harness()
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    target = next(m.tech_name for m in models if m.tech_name != h.state.meta.model)
    h.controller.change_model(target)
    assert h.wait(lambda: h.state.meta.model == target)
    assert h.client.get_session(h.session.id).model == target


def test_a_change_to_a_model_that_does_not_exist_is_reported(real, harness):
    h = harness()
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    h.controller.change_model("no-such-model")
    assert h.wait(lambda: h.of(c.ActionFailed))
    assert "The model could not be changed" in h.of(c.ActionFailed)[0].message and "Unknown model" in h.of(c.ActionFailed)[0].message
