import threading
import time

import pytest

pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from etcode_desktop.core import controller as c  # noqa: E402
from etcode_desktop.core.session_state import (  # noqa: E402
    NOTICE_CONNECTION, NOTICE_ERROR, NOTICE_LOAD, NOTICE_TOOLS, NOTICE_UNAVAILABLE, SessionState,
)
from etcode_desktop.core.sse import Backoff  # noqa: E402
from etcode_desktop.core.store import Scope, SessionStore  # noqa: E402
from fake_backend import Reply, StreamFault  # noqa: E402

FAST_BACKOFF = Backoff(initial=0.01, factor=1.5, maximum=0.05, give_up_after=5.0)


class Harness:
    """A controller on a fake backend, with every event recorded and applied to a SessionState."""

    def __init__(self, fake, store, make_client, session, token="tok-a", **kwargs):
        self.fake, self.store, self.session = fake, store, session
        self.scope = Scope.for_token(fake.url, token)
        self.client = make_client(fake, token=token)
        self.state = SessionState()
        self.events = []
        self._lock = threading.RLock()  # wait() evaluates conditions under it, and conditions call of()
        kwargs.setdefault("backoff", FAST_BACKOFF)
        kwargs.setdefault("retry_delays", (0.02,))
        kwargs.setdefault("poll_delays", (0.01, 0.02))
        self.controller = c.SessionController(self.client, store, self.scope, session.id, self._on_event, **kwargs)
        if store.get(self.scope, session.id) is None:
            from etcode_desktop.core.models import SessionMeta
            store.upsert_from_meta(self.scope, SessionMeta(id=session.id, model=session.model, status="idle"))

    def _on_event(self, event):
        with self._lock:
            self.events.append(event)
            self.state.apply(event)  # as the GUI thread will do. Guarded here, because threads emit

    def of(self, kind):
        with self._lock:
            return [e for e in self.events if isinstance(e, kind)]

    def wait(self, condition, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if condition():
                    return True
            time.sleep(0.005)
        with self._lock:
            return bool(condition())

    def settled(self, statuses=("idle",)):
        """Backend and controller agree that nothing is going on: every committed message was shown,
        no generation runs, nothing is being sent, and the follower is gone."""
        session = self.session
        count = len(session.messages)
        meta = self.state.meta
        return (meta is not None and meta.status in statuses and session.status in statuses
                and meta.message_count == count and set(range(count)) <= self.state.model.seen_indexes
                and all(g.finished for g in self.fake.generations if g.session is session)
                and not any(getattr(e, "state", "") == "sending" for e in self.state.model.entries())
                and not self.controller.is_following)

    def wait_idle(self, timeout=8.0, statuses=("idle",)):
        assert self.wait(lambda: self.settled(statuses), timeout), (
            f"not settled: status {self.state.status}, backend {self.session.status}, following {self.controller.is_following}, "
            f"{len(self.events)} events, the last ones: {[type(e).__name__ for e in self.events[-12:]]}")

    def texts(self):
        with self._lock:
            return [(e.role, e.text) for e in self.state.model.entries()]

    def send(self, text, **kw):
        key = self.state.begin_send(text)
        self.controller.send(key, text, **kw)
        return key


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "store.sqlite3")
    yield s
    s.close()


@pytest.fixture
def harness(fake, store, make_client):
    created = []

    def make(session=None, **kwargs):
        fake.auto_reply = True
        h = Harness(fake, store, make_client, session or fake.add_session(), **kwargs)
        created.append(h)
        return h

    yield make
    for h in created:
        h.controller.close()


# --- opening -----------------------------------------------------------------------------------------------

def test_open_loads_the_history_of_an_idle_session(harness, fake):
    session = fake.add_session()
    session.messages += [{"role": "user", "content": [{"type": "text", "text": "hello"}]},
                         {"role": "assistant", "text": {"type": "text", "text": "hi!"}, "reasoning": {"type": "text", "text": ""},
                          "tool_calls": [], "status": "complete", "error": None}]
    h = harness(session)
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    assert h.texts() == [("user", "hello"), ("assistant", "hi!")]
    assert h.state.status == "idle" and not h.controller.is_following
    loaded = h.of(c.HistoryLoaded)[0]
    assert [e.index for e in loaded.entries] == [0, 1, 2] and loaded.meta.id == session.id
    assert h.store.get(h.scope, session.id).last_seen_index == 2
    assert h.of(c.StoreUpdated)
    assert fake.stream_connections == 0  # nothing to follow


def test_open_continues_a_generation_that_is_already_running(harness, fake):
    session = fake.add_session()
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "question"}]})
    fake.replies.append(Reply(text="abcdef", chunks=6, delay=0.03))
    fake.start_generation  # noqa: B018 (the reply machinery starts the generation)
    fake._maybe_reply(session)
    h = harness(session)
    h.controller.open()
    h.wait_idle()
    assert h.texts() == [("user", "question"), ("assistant", "abcdef")]


def test_open_resumes_a_session_that_waits_for_tools(harness, fake):
    session = fake.add_session()
    session.status = "awaiting_tools"
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "go"}]})
    session.pending = [{"id": "c1", "name": "bash", "arguments": {"command": "ls"}}]
    h = harness(session)
    h.controller.open()
    assert h.wait(lambda: NOTICE_TOOLS in h.state.notices)
    pending = h.of(c.ToolsPending)[0]
    assert [(p.id, p.name) for p in pending.calls] == [("c1", "bash")]
    assert h.state.busy


def test_a_backend_without_pending_tool_calls_still_reports_the_wait(legacy_fake, store, make_client):
    legacy_fake.auto_reply = True
    session = legacy_fake.add_session()
    session.status = "awaiting_tools"
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "go"}]})
    session.pending = [{"id": "c1", "name": "bash", "arguments": {}}]
    h = Harness(legacy_fake, store, make_client, session)
    h.controller.open()
    assert h.wait(lambda: NOTICE_TOOLS in h.state.notices)
    assert h.of(c.ToolsPending)[0].calls == ()
    h.controller.close()


def test_open_failure_is_reported_and_refresh_can_retry(harness, fake):
    session = fake.add_session()
    h = harness(session)
    fake.fail_next("GET", f"/session/{session.id}$", status=503, times=10)
    h.controller.open()
    assert h.wait(lambda: NOTICE_LOAD in h.state.notices)
    assert not h.state.loaded and h.state.notices[NOTICE_LOAD].text
    fake.rest_faults.clear()
    h.controller.refresh()
    assert h.wait(lambda: h.state.loaded)
    assert NOTICE_LOAD not in h.state.notices


def test_an_unknown_session_is_marked_missing(harness, fake):
    session = fake.add_session()
    h = harness(session)
    del fake.sessions[session.id]
    h.controller.open()
    assert h.wait(lambda: NOTICE_UNAVAILABLE in h.state.notices)
    notice = h.state.notices[NOTICE_UNAVAILABLE]
    assert notice.missing and not h.state.can_send
    assert h.store.get(h.scope, session.id).is_missing


def test_a_rejected_token_makes_the_session_unavailable_but_not_missing(harness, fake, store, make_client):
    session = fake.add_session()
    h = harness(session, token="wrong")
    h.controller.open()
    assert h.wait(lambda: NOTICE_UNAVAILABLE in h.state.notices)
    assert not h.state.notices[NOTICE_UNAVAILABLE].missing and "rejected" in h.state.notices[NOTICE_UNAVAILABLE].text


# --- a conversation ----------------------------------------------------------------------------------------------

def test_send_and_stream_a_reply(harness, fake):
    h = harness()
    fake.replies.append(Reply(text="Hello there!", reasoning="thinking...", chunks=4, delay=0.01, cost="0.25"))
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    key = h.send("hi")
    h.wait_idle()
    assert h.texts() == [("user", "hi"), ("assistant", "Hello there!")]
    user_entry, assistant_entry = h.state.model.entries()
    assert user_entry.index == 1 and user_entry.state == "committed" and user_entry.key == key
    assert assistant_entry.index == 2 and assistant_entry.reasoning == "thinking..." and assistant_entry.status == "complete"
    assert not assistant_entry.is_live and h.state.cost_total == "0.25"
    sent = h.of(c.UserSent)[0]
    assert (sent.local_key, sent.index, sent.queued) == (key, 1, False)
    assert len(h.of(c.LiveText)) >= 2  # it streamed
    committed = h.of(c.Committed)
    assert any(e.index == 2 and e.generation_id for e in committed)  # the final message names its generation
    assert h.of(c.GenerationEnded)[-1].status == "complete"
    assert h.state.cost_text() == "$0.25"


def test_the_first_message_gives_the_session_its_title(harness, fake):
    h = harness()
    h.send("  Please fix\nthe build  ")
    h.wait_idle()
    assert h.store.get(h.scope, h.session.id).title == "Please fix the build"


def test_send_passes_the_inference_options(harness, fake):
    h = harness()
    h.send("hi", temperature=0.2, max_tokens=50, reasoning_effort="low")
    h.wait_idle()
    body = fake.requests_to("POST", "/messages$")[0].json
    assert (body["temperature"], body["max_tokens"], body["reasoning_effort"]) == (0.2, 50, "low")


def test_a_second_message_after_the_first_is_answered(harness, fake):
    h = harness()
    fake.replies.extend([Reply(text="one"), Reply(text="two")])
    h.send("first")
    h.wait_idle()
    h.send("second")
    h.wait(lambda: len(h.state.model.entries()) == 4)
    h.wait_idle()
    assert h.texts() == [("user", "first"), ("assistant", "one"), ("user", "second"), ("assistant", "two")]
    assert [e.index for e in h.state.model.entries()] == [1, 2, 3, 4]


def test_messages_sent_while_the_model_works_are_queued_and_answered_in_order(harness, fake):
    h = harness()
    fake.replies.extend([Reply(text="first answer", chunks=5, delay=0.05), Reply(text="second answer")])
    h.send("one")
    assert h.wait(lambda: h.state.busy)
    second = h.send("two")
    assert h.wait(lambda: h.of(c.UserSent) and len(h.of(c.UserSent)) == 2)
    sent_two = [e for e in h.of(c.UserSent) if e.local_key == second][0]
    assert sent_two.queued is True and sent_two.index is None
    h.wait_idle()
    assert h.texts() == [("user", "one"), ("assistant", "first answer"), ("user", "two"), ("assistant", "second answer")]
    assert not [e for e in h.state.model.entries() if getattr(e, "state", "committed") != "committed"]  # nothing is left pending


def test_the_generation_may_be_over_before_the_stream_is_opened(harness, fake):
    # a reply that finishes at once: the follower finds no stream (204) and the history fills the gap
    h = harness()
    fake.replies.append(Reply(text="quick"))
    original = h.client.open_stream

    def late(session_id, last_event_id=None):
        h.wait(lambda: h.session.active is None or h.session.active.finished, 2.0)
        h.session.active = None
        return original(session_id, last_event_id)

    h.client.open_stream = late
    h.send("hi")
    h.wait_idle()
    assert h.texts() == [("user", "hi"), ("assistant", "quick")]
    assert not h.of(c.LiveText)  # nothing was streamed: the message came from the history


def test_two_quick_messages_are_both_answered_whatever_the_timing(harness, fake):
    for round_number in range(15):
        session = fake.add_session()
        h = harness(session)
        fake.replies.extend([Reply(text=f"a{round_number}"), Reply(text=f"b{round_number}")])
        h.send("x")
        h.send("y")
        h.wait_idle()
        texts = [t for r, t in h.texts() if r == "assistant"]
        assert texts == [f"a{round_number}", f"b{round_number}"], f"round {round_number}: {h.texts()}"
        h.controller.close()


# --- failures when sending --------------------------------------------------------------------------------------------

def test_a_refused_message_is_reported_and_not_retried(harness, fake):
    h = harness()
    fake.fail_next("POST", "/messages$", status=400, code="validation_error", message="content must not be empty", times=5)
    key = h.send("hi")
    assert h.wait(lambda: h.of(c.UserFailed))
    failed = h.of(c.UserFailed)[0]
    assert failed.local_key == key and not failed.unknown_outcome and "content must not be empty" in failed.message
    assert h.state.model.get(key).state == "failed"
    assert len(fake.requests_to("POST", "/messages$")) == 1


def test_a_message_lost_to_a_server_error_has_an_unknown_outcome_and_is_not_retried(harness, fake):
    h = harness()
    fake.fail_next("POST", "/messages$", status=503, times=5)
    key = h.send("hi")
    assert h.wait(lambda: h.of(c.UserFailed))
    assert h.of(c.UserFailed)[0].unknown_outcome
    assert h.state.model.get(key).state == "unknown"
    assert len(fake.requests_to("POST", "/messages$")) == 1


def test_an_unknown_outcome_message_is_replaced_when_it_turns_up_in_the_history(harness, fake):
    h = harness()
    fake.replies.append(Reply(text="answer"))
    original = h.client.submit

    def flaky(session_id, messages, **kw):
        original(session_id, messages, **kw)  # the backend gets it...
        from etcode_desktop.core.errors import Unreachable
        raise Unreachable("answer lost")  # ...but we never hear about it

    h.client.submit = flaky
    key = h.send("hello")
    assert h.wait(lambda: h.of(c.UserFailed))
    h.client.submit = original
    h.controller.open()  # the user reconnects and the history is loaded again
    h.wait_idle()
    assert h.texts() == [("user", "hello"), ("assistant", "answer")]
    assert h.state.model.get(key) is None


def test_sending_to_a_vanished_session_marks_it_missing(harness, fake):
    h = harness()
    del fake.sessions[h.session.id]
    h.send("hi")
    assert h.wait(lambda: NOTICE_UNAVAILABLE in h.state.notices)
    assert h.of(c.UserFailed) and h.store.get(h.scope, h.session.id).is_missing


# --- stopping -------------------------------------------------------------------------------------------------------------------

def test_stop_cancels_a_running_generation(harness, fake):
    h = harness()
    fake.replies.append(Reply(text="x" * 60, chunks=60, delay=0.05))
    h.send("go")
    assert h.wait(lambda: h.of(c.LiveText))
    h.controller.stop()
    h.wait_idle()
    assistant = h.state.model.entries()[-1]
    assert assistant.status == "cancelled" and assistant.text.startswith("x") and "[cancelled]" in assistant.text
    assert h.of(c.GenerationCancelled) and h.of(c.GenerationEnded)[-1].status == "cancelled"
    assert 0 < len(assistant.text) < 60


def test_stop_with_nothing_to_stop_is_quiet(harness, fake):
    h = harness()
    h.controller.stop()
    assert h.wait(lambda: h.of(c.StatusChanged))
    time.sleep(0.1)
    assert not h.of(c.ActionFailed) and not h.state.notices


def test_a_failed_stop_is_reported(harness, fake):
    h = harness()
    fake.fail_next("POST", "/cancel$", status=500, code="boom", message="it broke", times=5)
    h.controller.stop()
    assert h.wait(lambda: NOTICE_ERROR in h.state.notices)
    assert "Could not stop" in h.state.notices[NOTICE_ERROR].text and "it broke" in h.state.notices[NOTICE_ERROR].text


def test_stop_then_send_works(harness, fake):
    h = harness()
    fake.replies.extend([Reply(text="y" * 40, chunks=40, delay=0.05), Reply(text="fresh")])
    h.send("one")
    assert h.wait(lambda: h.of(c.LiveText))
    h.controller.stop()
    h.wait_idle()
    h.send("two")
    h.wait(lambda: len(h.state.model.entries()) == 4)
    h.wait_idle()
    assert [r for r, _ in h.texts()] == ["user", "assistant", "user", "assistant"] and h.texts()[-1] == ("assistant", "fresh")


# --- errors in a generation --------------------------------------------------------------------------------------------------------

def test_a_failed_generation_shows_the_error_and_the_session_can_go_on(harness, fake):
    h = harness()
    fake.replies.extend([Reply(text="partial", start_delay=0.15, error={"code": "upstream_error", "message": "provider fell over", "retryable": True}),
                         Reply(text="recovered")])
    h.send("go")
    assert h.wait(lambda: h.state.status == "failed" and not h.controller.is_following)
    failed = h.state.model.entries()[-1]
    assert failed.status == "error" and failed.error["message"] == "provider fell over" and failed.text == "partial"
    assert h.of(c.GenerationFailed)[0].error["code"] == "upstream_error"
    h.send("try again")
    assert h.wait(lambda: h.state.status == "idle" and not h.controller.is_following and len(h.state.model.entries()) == 4)
    assert h.texts()[-1] == ("assistant", "recovered")


# --- tool calls -----------------------------------------------------------------------------------------------------------------------

def test_tool_calls_are_shown_and_the_session_waits(harness, fake):
    h = harness()
    fake.replies.append(Reply(text="Let me look.", start_delay=0.15, tool_calls=[("c1", "bash", {"command": "ls"}), ("c2", "read_file", {"path": "a.py"})]))
    h.send("what is here?")
    assert h.wait(lambda: NOTICE_TOOLS in h.state.notices)
    assert h.wait(lambda: not h.controller.is_following)
    assistant = h.state.model.entries()[-1]
    assert assistant.text == "Let me look." and assistant.index == 2
    assert [(t.id, t.name, t.arguments) for t in assistant.tool_calls] == [
        ("c1", "bash", {"command": "ls"}), ("c2", "read_file", {"path": "a.py"})]
    assert [t.result for t in assistant.tool_calls] == [None, None]
    assert h.state.status == "awaiting_tools" and h.state.busy
    assert [p.id for p in h.of(c.ToolsPending)[0].calls] == ["c1", "c2"]
    assert h.of(c.LiveToolCall)  # they were seen while streaming


def test_stop_gives_up_on_tool_calls_and_shows_their_cancelled_results(harness, fake):
    h = harness()
    fake.replies.append(Reply(tool_calls=[("c1", "bash", {"command": "sleep 100"})]))
    h.send("run it")
    assert h.wait(lambda: NOTICE_TOOLS in h.state.notices)
    h.controller.stop()
    h.wait_idle()
    call = h.state.model.entries()[-1].tool_calls[0]
    assert call.result and "cancelled" in call.result[0]["text"].lower()
    assert NOTICE_TOOLS not in h.state.notices and not h.state.busy


def test_a_web_search_that_the_backend_runs_continues_by_itself(harness, fake):
    h = harness()
    fake.replies.extend([Reply(text="Searching.", start_delay=0.15, tool_calls=[("w1", "websearch", {"query": "python"})], search_result="1. Python.org"),
                         Reply(text="Python is a language.")])
    h.send("what is python?")
    h.wait_idle()
    entries = h.state.model.entries()
    assert [e.role for e in entries] == ["user", "assistant", "assistant"]
    first = entries[1]
    assert first.tool_calls[0].name == "websearch" and first.tool_calls[0].result[0]["text"] == "1. Python.org"
    assert entries[2].text == "Python is a language."
    assert h.of(c.LiveToolResult) and NOTICE_TOOLS not in h.state.notices
    assert [e.index for e in entries] == [1, 2, 4]  # index 3 is the tool message, which is not shown


def test_with_a_web_search_and_another_tool_only_the_other_one_is_pending(harness, fake):
    h = harness()
    fake.replies.append(Reply(tool_calls=[("w1", "websearch", {"query": "q"}), ("c1", "bash", {"command": "ls"})]))
    h.send("both")
    assert h.wait(lambda: NOTICE_TOOLS in h.state.notices)
    assert [p.id for p in h.of(c.ToolsPending)[0].calls] == ["c1"]


# --- connection trouble ----------------------------------------------------------------------------------------------------------------------

def test_a_dropped_stream_is_resumed_without_losing_or_repeating_text(harness, fake):
    h = harness()
    fake.replies.append(Reply(text="".join(f"{i}," for i in range(12)), chunks=12, delay=0.03))
    fake.stream_faults.append(StreamFault(cut_after=3, abrupt=True))
    h.send("go")
    h.wait_idle()
    assert h.texts()[-1] == ("assistant", "".join(f"{i}," for i in range(12)))
    assert h.of(c.ConnectionLost) and h.of(c.ConnectionRestored)
    assert NOTICE_CONNECTION not in h.state.notices  # it came back


def test_a_connection_that_stays_down_for_a_while_comes_back_and_continues(harness, fake):
    text = "".join(f"{i}," for i in range(14))
    h = harness(backoff=Backoff(initial=0.01, factor=1.0, maximum=0.01, give_up_after=0.08))
    fake.replies.append(Reply(text=text, chunks=14, delay=0.04))
    fake.stream_faults.append(StreamFault(cut_after=2, abrupt=True))
    fake.stream_faults.extend(StreamFault(status=503) for _ in range(40))  # the follower gives up, the controller retries
    h.send("go")
    assert h.wait(lambda: len(h.of(c.ConnectionLost)) >= 2)
    assert NOTICE_CONNECTION in h.state.notices
    fake.stream_faults.clear()
    h.wait_idle(timeout=10)
    assert h.texts()[-1] == ("assistant", text)  # complete and not repeated
    assert NOTICE_CONNECTION not in h.state.notices


def test_a_status_poll_that_fails_is_retried(harness, fake):
    h = harness()
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    session = h.session
    fake.fail_next("GET", f"/session/{session.id}$", status=503, times=3)  # all three tries of one status request
    h.send("hi")
    h.wait_idle()
    assert h.texts()[-1][0] == "assistant"
    assert h.of(c.ConnectionLost) and h.of(c.ConnectionRestored)


def test_polling_while_the_backend_says_generating_stops_when_it_is_over(harness, fake):
    h = harness()
    session = h.session
    session.status = "generating"  # generating, but there is no stream to follow
    h.controller.open()
    assert h.wait(lambda: h.controller.is_following)
    time.sleep(0.15)
    assert len(fake.requests_to("GET", f"/session/{session.id}$")) >= 3  # it keeps looking, softly
    session.status = "idle"
    h.wait_idle()


# --- closing ---------------------------------------------------------------------------------------------------------------------------------------

def test_close_stops_following_at_once_and_silences_events(harness, fake):
    h = harness()
    fake.replies.append(Reply(text="z" * 100, chunks=100, delay=0.05))
    h.send("long")
    assert h.wait(lambda: h.of(c.LiveText))
    h.controller.close()
    assert h.wait(lambda: not h.controller.is_following, 2.0)
    count = len(h.events)
    time.sleep(0.2)
    assert len(h.events) == count


def test_a_broken_event_handler_does_not_kill_the_controller(harness, fake):
    h = harness()
    calls = []

    def bad(event):
        calls.append(event)
        raise RuntimeError("view exploded")

    h.controller._emit_callback = bad
    fake.replies.append(Reply(text="ok", start_delay=0.15))  # so that the stream is open when it starts
    h.send("hi")
    assert h.wait(lambda: any(isinstance(e, c.GenerationEnded) for e in calls), 5.0)


# --- the follower thread -----------------------------------------------------------------------------------------------------------------------------------

def test_a_request_to_follow_while_the_follower_is_finishing_is_not_lost(harness, fake):
    h = harness()
    calls, gate = [], threading.Event()

    def once():
        calls.append(1)
        if len(calls) == 1:
            gate.wait(3)  # the follower is busy (or just about to finish) when the second request comes
        return False

    h.controller._follow_once = once
    h.controller._ensure_following()
    assert h.wait(lambda: len(calls) == 1)
    h.controller._ensure_following()  # must not start a second thread, and must not be forgotten
    assert threading.active_count() >= 1
    gate.set()
    assert h.wait(lambda: len(calls) == 2 and not h.controller.is_following)
    h.controller._ensure_following()  # a later request starts a new follower
    assert h.wait(lambda: len(calls) == 3 and not h.controller.is_following)


def test_only_one_follower_runs_at_a_time(harness, fake):
    h = harness()
    running, peak, lock = [0], [0], threading.Lock()

    def once():
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return False

    h.controller._follow_once = once
    for _ in range(20):
        h.controller._ensure_following()
    assert h.wait(lambda: not h.controller.is_following, 5.0)
    assert peak[0] == 1


def test_an_unexpected_error_in_the_follower_is_reported_not_swallowed(harness, fake):
    h = harness()

    def boom():
        raise RuntimeError("something unexpected")

    h.controller._follow_once = boom
    h.controller._ensure_following()
    assert h.wait(lambda: h.of(c.ActionFailed))
    assert "something unexpected" in h.of(c.ActionFailed)[0].message
    assert h.wait(lambda: not h.controller.is_following)


def test_close_can_wait_for_the_follower_to_finish(harness, fake):
    h = harness()
    fake.replies.append(Reply(text="z" * 100, chunks=100, delay=0.05))
    h.send("long")
    assert h.wait(lambda: h.of(c.LiveText))
    started = time.monotonic()
    h.controller.close(wait=3.0)
    assert not h.controller.is_following and time.monotonic() - started < 2.0
