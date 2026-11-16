"""The controller runs the tool calls of the model: in parallel, journaled, posted as they finish, safe to restart."""
import os
import threading
import time

import pytest

pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from etcode_desktop.core import controller as c  # noqa: E402
from etcode_desktop.core.models import PendingToolCall  # noqa: E402
from etcode_desktop.core.store import JOURNAL_DONE, JOURNAL_POSTED, JOURNAL_RUNNING, SessionStore  # noqa: E402
from etcode_desktop.core.tools.scheduler import ToolPool, ToolRunner  # noqa: E402
from fake_backend import PNG, Reply  # noqa: E402
from test_controller import Harness  # noqa: E402


class Stub:
    """A tool executor with a stopwatch: ``sleep`` in the arguments says how long a call takes."""

    def __init__(self):
        self.calls, self.finished, self.running, self.peak = [], [], 0, 0
        self.lock = threading.Lock()

    def __call__(self, name, arguments, context):
        with self.lock:
            self.calls.append((name, arguments.get("tag", "")))
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            context.cancel.wait(arguments.get("sleep", 0))
            if arguments.get("boom"):
                raise RuntimeError("the executor broke")
            return [{"type": "text", "text": f"ran {name} {arguments.get('tag', '')}"}]
        finally:
            with self.lock:
                self.running -= 1
                self.finished.append(arguments.get("tag", ""))


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "store.sqlite3")
    yield s
    s.close()


@pytest.fixture
def make(fake, store, make_client, tmp_path):
    created = []

    def factory(session=None, execute=None, pool_size=8, **kwargs):
        fake.auto_reply = True
        runner = ToolRunner(ToolPool(pool_size), **({"execute": execute} if execute else {}), home=tmp_path)
        harness = Harness(fake, store, make_client, session or fake.add_session(), tools=runner, **kwargs)
        harness.runner = runner
        created.append(harness)
        return harness

    yield factory
    for harness in created:
        harness.controller.close()


def call(call_id, name="bash", **arguments):
    return (call_id, name, arguments)


def tool_messages(session):
    return [m for m in session.messages if m["role"] == "tool"]


def posts_of_tool_results(fake):
    return [r.json["messages"][0]["tool_call_id"] for r in fake.requests_to("POST", "/messages$")
            if r.json and r.json["messages"][0]["role"] == "tool"]


def settle(h, timeout=15):
    h.wait_idle(timeout)


# --- a tool round ---------------------------------------------------------------------------------------------------------------------

def test_a_command_is_run_posted_and_the_model_goes_on(make, fake, tmp_path):
    h = make()
    fake.replies.extend([Reply(text="Let me check.", start_delay=0.1, tool_calls=[call("c1", command="echo hi", directory=str(tmp_path))]),
                         Reply(text="It printed hi.")])
    h.send("say hi with the shell")
    settle(h)
    assert tool_messages(h.session)[0]["content"] == [{"type": "text", "text": "<returncode>0</returncode>\n<stderr></stderr>\n<stdout>hi\n</stdout>"}]
    assert h.texts()[-1] == ("assistant", "It printed hi.")
    entries = h.state.model.entries()
    assert [e.role for e in entries] == ["user", "assistant", "assistant"]
    card = entries[1].tool_calls[0]
    assert card.result[0]["text"].endswith("<stdout>hi\n</stdout>") and not card.running and card.duration is not None
    assert h.of(c.ToolStarted)[0].call_id == "c1" and h.of(c.ToolFinished)[0].call_id == "c1"
    assert h.of(c.ToolFinished)[0].duration > 0
    assert h.store.journal_get(h.scope, h.session.id, "c1").state == JOURNAL_POSTED
    assert not h.state.notices


def test_the_events_come_in_a_sensible_order(make, fake, tmp_path):
    h = make()
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1", command="true", directory=str(tmp_path))]), Reply(text="done")])
    h.send("go")
    settle(h)
    kinds = [type(e).__name__ for e in h.events]
    assert kinds.index("LiveToolCall") < kinds.index("ToolStarted") < kinds.index("ToolFinished")
    assert kinds.index("Committed") < kinds.index("ToolStarted")  # the assistant message was committed before the call ran


def test_several_rounds_in_a_row(make, fake):
    h = make(execute=Stub())
    fake.replies.extend([Reply(text="r1", start_delay=0.05, tool_calls=[call("a1", tag="A")]),
                         Reply(text="r2", start_delay=0.05, tool_calls=[call("b1", tag="B"), call("b2", tag="C")]),
                         Reply(text="all done")])
    h.send("go")
    settle(h)
    assert h.texts()[-1] == ("assistant", "all done")
    assert [m["tool_call_id"] for m in tool_messages(h.session)] == ["a1", "b1", "b2"]
    assert {e.call_id for e in h.of(c.ToolFinished)} == {"a1", "b1", "b2"}


# --- parallel -------------------------------------------------------------------------------------------------------------------------

def test_calls_run_side_by_side_and_are_posted_as_they_finish(make, fake):
    stub = Stub()
    h = make(execute=stub)
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1", tag="slow", sleep=0.8), call("c2", tag="fast", sleep=0.1),
                                                                       call("c3", tag="middle", sleep=0.4)]), Reply(text="done")])
    started = time.monotonic()
    h.send("three things")
    settle(h)
    assert time.monotonic() - started < 2.5, "the calls ran one after the other"
    assert stub.peak == 3 and stub.finished == ["fast", "middle", "slow"]
    assert posts_of_tool_results(fake) == ["c2", "c3", "c1"]  # in the order they finished
    assert [m["tool_call_id"] for m in tool_messages(h.session)] == ["c1", "c2", "c3"]  # the history is in call order
    assert h.texts()[-1] == ("assistant", "done")


def test_the_cards_show_each_call_on_its_own(make, fake):
    h = make(execute=Stub())
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1", tag="slow", sleep=2.5), call("c2", tag="fast")]), Reply(text="done")])
    h.send("go")
    def find_call(call_id):
        return next((t for e in h.state.model.entries() if e.role == "assistant" for t in e.tool_calls if t.id == call_id), None)

    assert h.wait(lambda: find_call("c2") is not None and find_call("c2").result and find_call("c1") is not None and find_call("c1").running)
    entry = next(e for e in h.state.model.entries() if e.role == "assistant")
    states = {t.id: (t.running, t.result is not None) for t in entry.tool_calls}
    assert states == {"c1": (True, False), "c2": (False, True)}  # one is still running while the other is done
    settle(h)


def test_a_write_and_a_read_of_the_same_file_in_one_message_are_in_order(make, fake, tmp_path):
    h = make()
    path = tmp_path / "notes.txt"
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[
        call("w", "write_to_file", path=str(path), content="first line\n"),
        call("r", "read_file", path=str(path)),
        call("e", "replace_in_file", path=str(path), search="first", replace="second"),
        call("r2", "read_file", path=str(path)),
        call("other", "bash", command="echo unrelated", directory=str(tmp_path))]), Reply(text="done")])
    h.send("go")
    settle(h)
    results = {m["tool_call_id"]: m["content"][0]["text"] for m in tool_messages(h.session)}
    assert results["w"] == f"Wrote 11 characters to {path}"
    assert "<content>first line\n</content>" in results["r"]  # the read saw what the write wrote
    assert "Made 1 replacement(s)" in results["e"]
    assert "<content>second line\n</content>" in results["r2"]
    assert "unrelated" in results["other"]


def test_the_pool_limits_how_many_calls_run_at_once(make, fake):
    stub = Stub()
    h = make(execute=stub, pool_size=2)
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call(f"c{i}", tag=str(i), sleep=0.2) for i in range(6)]), Reply(text="done")])
    h.send("many")
    settle(h)
    assert stub.peak == 2 and len(stub.calls) == 6
    assert h.runner.pool.thread_count == 2


# --- mixed with web search and bad calls --------------------------------------------------------------------------------------------

def test_the_backends_web_search_is_left_to_the_backend(make, fake, tmp_path):
    stub = Stub()
    h = make(execute=stub)
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("w", "websearch", query="python"), call("b", tag="bash-call")],
                               search_result="1. python.org"), Reply(text="done")])
    h.send("search and run")
    settle(h)
    assert stub.calls == [("bash", "bash-call")]  # websearch was not run here
    assert posts_of_tool_results(fake) == ["b"]
    assert h.store.journal_get(h.scope, h.session.id, "w") is None
    contents = {m["tool_call_id"]: m["content"][0]["text"] for m in tool_messages(h.session)}
    assert contents == {"w": "1. python.org", "b": "ran bash bash-call"}


def test_a_call_with_bad_arguments_is_answered_with_an_error_and_the_session_goes_on(make, fake):
    h = make()
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[("c1", "bash", {}), ("c2", "no_such_tool", {"a": 1}),
                                                                       ("c3", "read_file", {"path": ["not", "a", "string"]})]), Reply(text="ok")])
    h.send("go")
    settle(h)
    texts = {m["tool_call_id"]: m["content"][0]["text"] for m in tool_messages(h.session)}
    assert texts["c1"] == "Error: invalid call of bash: missing required argument 'command'"
    assert texts["c2"].startswith("Error: invalid call of no_such_tool: unknown tool 'no_such_tool'")
    assert texts["c3"] == "Error: invalid call of read_file: argument 'path' must be a string"
    assert h.texts()[-1] == ("assistant", "ok")
    entry = next(e for e in h.state.model.entries() if e.role == "assistant" and e.tool_calls)
    assert all(t.result[0]["text"].startswith("Error: ") for t in entry.tool_calls)


def test_an_executor_that_breaks_still_gives_the_model_an_answer(make, fake):
    h = make(execute=Stub())
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1", boom=True), call("c2", tag="fine")]), Reply(text="recovered")])
    h.send("go")
    settle(h)
    texts = {m["tool_call_id"]: m["content"][0]["text"] for m in tool_messages(h.session)}
    assert texts["c1"] == "Error: the tool bash failed: RuntimeError: the executor broke" and texts["c2"] == "ran bash fine"
    assert h.texts()[-1] == ("assistant", "recovered")


def test_an_image_is_uploaded_and_its_hash_is_the_result(make, fake, tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    h = make(session=fake.add_session(model="fake-coder"))
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1", "view_image", path="shot.png")]), Reply(text="nice")])
    h.send("look")
    settle(h)
    content = tool_messages(h.session)[0]["content"]
    assert content[0]["type"] == "image" and len(content[0]["hash"]) == 64 and content[1]["text"].startswith("Image shot.png (image/png")
    assert ("tok-a", content[0]["hash"]) in fake.images


def test_very_long_results_are_cut_below_the_backends_limit(make, fake):
    h = make(execute=lambda name, arguments, context: [{"type": "text", "text": "x" * 2_500_000}])
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1")]), Reply(text="done")])
    h.send("go")
    settle(h)
    text = tool_messages(h.session)[0]["content"][0]["text"]
    assert len(text) < 1_950_000 and "[result truncated: 600000 more characters]" in text


def test_limit_and_empty_results():
    parts = [{"type": "text", "text": "a" * 10}, {"type": "text", "text": "b" * 10}]
    assert c._limit(parts, 100) == parts
    cut = c._limit(parts, 15)
    assert cut[0]["text"] == "a" * 10 and cut[1]["text"].startswith("b" * 5 + "\n[result truncated: 5 more characters]")
    assert c._limit([], 100) == [{"type": "text", "text": "(no output)"}]
    image = {"type": "image", "hash": "h" * 64}
    assert c._limit([image], 5) == [image]


# --- stopping -------------------------------------------------------------------------------------------------------------------------------

def test_stop_ends_running_commands_and_the_session_goes_idle(make, fake, tmp_path):
    h = make()
    pid_file = tmp_path / "pid"
    fake.replies.append(Reply(text="x", start_delay=0.1, tool_calls=[call("c1", command=f"sleep 60 & echo $! > {pid_file}; sleep 60", directory=str(tmp_path)),
                                                                      call("c2", command="sleep 60", directory=str(tmp_path))]))
    h.send("run long things")
    assert h.wait(lambda: len(h.of(c.ToolStarted)) == 2 and pid_file.exists())
    started = time.monotonic()
    h.controller.stop()
    h.wait_idle(15)
    assert time.monotonic() - started < 8
    assert not posts_of_tool_results(fake), "a stopped call must not post a result"
    assert fake.requests_to("POST", "/cancel$")
    time.sleep(0.3)
    pid = int(pid_file.read_text())
    assert not os.path.exists(f"/proc/{pid}") or "Z (zombie)" in open(f"/proc/{pid}/status").read()
    results = {m["tool_call_id"]: m["content"][0]["text"] for m in tool_messages(h.session)}
    assert all("cancelled" in text.lower() for text in results.values())  # the backend's own note
    entry = next(e for e in h.state.model.entries() if e.role == "assistant" and e.tool_calls)
    assert all(t.result and not t.running for t in entry.tool_calls)
    assert not h.controller.is_following


def test_stop_then_a_new_message_works(make, fake, tmp_path):
    h = make()
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1", command="sleep 60", directory=str(tmp_path))]), Reply(text="fresh start")])
    h.send("go")
    assert h.wait(lambda: h.of(c.ToolStarted))
    h.controller.stop()
    h.wait_idle(15)
    h.send("try something else")
    assert h.wait(lambda: h.texts()[-1] == ("assistant", "fresh start"), 10)
    h.wait_idle(15)


def test_stop_while_the_batch_is_starting_does_not_leave_a_command_behind(make, fake, tmp_path):
    h = make()
    fake.replies.append(Reply(text="x", start_delay=0.1, tool_calls=[call(f"c{i}", command="sleep 60", directory=str(tmp_path)) for i in range(5)]))
    h.send("go")
    assert h.wait(lambda: h.of(c.ToolStarted))
    h.controller.stop()
    h.wait_idle(15)
    assert h.runner.pool.thread_count >= 1
    time.sleep(0.5)
    assert h.controller._inflight == {}


def test_closing_the_controller_ends_the_commands(make, fake, tmp_path):
    h = make()
    pid_file = tmp_path / "pid"
    fake.replies.append(Reply(text="x", start_delay=0.1, tool_calls=[call("c1", command=f"sleep 60 & echo $! > {pid_file}; sleep 60", directory=str(tmp_path))]))
    h.send("go")
    assert h.wait(lambda: pid_file.exists())
    h.controller.close(wait=5)
    time.sleep(0.5)
    pid = int(pid_file.read_text())
    assert not os.path.exists(f"/proc/{pid}") or "Z (zombie)" in open(f"/proc/{pid}/status").read()


# --- the journal and restarts ------------------------------------------------------------------------------------------------------------

def awaiting_session(fake, calls):
    """A session that waits for tool results, as after a restart of the app."""
    session = fake.add_session()
    session.status = "awaiting_tools"
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "do things"}]})
    session.messages.append({"role": "assistant", "text": {"type": "text", "text": "ok"}, "reasoning": {"type": "text", "text": ""},
                             "tool_calls": [{"id": i, "name": n, "arguments": a} for i, n, a in calls], "status": "complete", "error": None})
    session.pending = [{"id": i, "name": n, "arguments": a} for i, n, a in calls]
    return session


def journal(h, call_id, name, arguments, state, result=None):
    h.store.journal_start(h.scope, h.session.id, call_id, name, arguments)
    if state in (JOURNAL_DONE, JOURNAL_POSTED):
        h.store.journal_done(h.scope, h.session.id, call_id, result)
    if state == JOURNAL_POSTED:
        h.store.journal_posted(h.scope, h.session.id, call_id)


def test_a_result_that_was_not_posted_is_posted_again_without_running_the_call_again(make, fake):
    stub = Stub()
    session = awaiting_session(fake, [call("c1", tag="T")])
    h = make(session, execute=stub)
    journal(h, "c1", "bash", {"tag": "T"}, JOURNAL_DONE, [{"type": "text", "text": "the earlier result"}])
    fake.replies.append(Reply(text="thanks"))
    h.controller.open()
    settle(h)
    assert stub.calls == []  # never run again
    assert tool_messages(session)[0]["content"] == [{"type": "text", "text": "the earlier result"}]
    assert h.texts()[-1] == ("assistant", "thanks")
    assert h.store.journal_get(h.scope, session.id, "c1").state == JOURNAL_POSTED


def test_a_call_that_was_running_when_the_app_died_is_not_run_again(make, fake):
    stub = Stub()
    session = awaiting_session(fake, [call("c1", tag="T")])
    h = make(session, execute=stub)
    journal(h, "c1", "bash", {"tag": "T"}, JOURNAL_RUNNING)
    fake.replies.append(Reply(text="I see."))
    h.controller.open()
    settle(h)
    assert stub.calls == []
    text = tool_messages(session)[0]["content"][0]["text"]
    assert text.startswith("Error: the desktop client was interrupted while executing this tool call") and "outcome is unknown" in text
    assert h.store.journal_get(h.scope, session.id, "c1").state == JOURNAL_POSTED


def test_a_call_that_was_never_started_is_run(make, fake):
    stub = Stub()
    session = awaiting_session(fake, [call("c1", tag="T")])
    h = make(session, execute=stub)
    fake.replies.append(Reply(text="done"))
    h.controller.open()
    settle(h)
    assert stub.calls == [("bash", "T")] and tool_messages(session)[0]["content"][0]["text"] == "ran bash T"


def test_a_posted_call_that_is_still_unresolved_is_posted_again(make, fake):
    stub = Stub()
    session = awaiting_session(fake, [call("c1", tag="T")])
    h = make(session, execute=stub)
    journal(h, "c1", "bash", {"tag": "T"}, JOURNAL_POSTED, [{"type": "text", "text": "posted before, but the backend lost it"}])
    fake.replies.append(Reply(text="done"))
    h.controller.open()
    settle(h)
    assert stub.calls == [] and tool_messages(session)[0]["content"][0]["text"] == "posted before, but the backend lost it"


def test_after_a_restart_each_call_gets_the_right_treatment_side_by_side(make, fake):
    stub = Stub()
    session = awaiting_session(fake, [call("c1", tag="1"), call("c2", tag="2"), call("c3", tag="3"), call("c4", tag="4")])
    h = make(session, execute=stub)
    journal(h, "c1", "bash", {"tag": "1"}, JOURNAL_DONE, [{"type": "text", "text": "kept 1"}])
    journal(h, "c2", "bash", {"tag": "2"}, JOURNAL_RUNNING)
    journal(h, "c4", "bash", {"tag": "4"}, JOURNAL_POSTED, [{"type": "text", "text": "kept 4"}])
    fake.replies.append(Reply(text="done"))
    h.controller.open()
    settle(h)
    assert stub.calls == [("bash", "3")]  # only the call that was never started
    results = {m["tool_call_id"]: m["content"][0]["text"] for m in tool_messages(session)}
    assert results["c1"] == "kept 1" and results["c3"] == "ran bash 3" and results["c4"] == "kept 4"
    assert results["c2"].startswith("Error: the desktop client was interrupted")


def test_a_second_controller_for_the_same_session_does_not_run_a_call_twice(make, fake):
    stub = Stub()
    session = awaiting_session(fake, [call("c1", tag="T", sleep=0.3)])
    h = make(session, execute=stub)
    fake.replies.append(Reply(text="done"))
    h.controller.open()
    assert h.wait(lambda: h.of(c.ToolStarted))
    h.controller.refresh()  # opening again while the call runs
    settle(h)
    assert stub.calls == [("bash", "T")]


# --- the connection --------------------------------------------------------------------------------------------------------------------------

def test_a_result_that_cannot_be_posted_is_kept_and_posted_when_the_backend_answers_again(make, fake):
    stub = Stub()
    h = make(execute=stub)
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1", tag="T", sleep=0.4)]), Reply(text="done")])
    h.send("go")
    assert h.wait(lambda: h.of(c.ToolStarted))
    fake.fail_next("POST", "/messages$", status=503, times=3)  # all three tries of the first post, which comes when the call is done
    settle(h, 20)
    assert h.of(c.ConnectionLost) and h.of(c.ConnectionRestored)
    assert stub.calls == [("bash", "T")]  # run once, whatever happened to the post
    assert tool_messages(h.session)[0]["content"][0]["text"] == "ran bash T"
    assert h.texts()[-1] == ("assistant", "done")


def test_a_result_that_the_backend_already_has_is_not_an_error(make, fake):
    session = awaiting_session(fake, [call("c1", tag="T"), call("c2", tag="U")])
    h = make(session, execute=Stub())
    session.pending[0]["result"] = [{"type": "text", "text": "someone answered this already"}]  # accepted, the barrier waits for c2
    journal(h, "c1", "bash", {"tag": "T"}, JOURNAL_DONE, [{"type": "text", "text": "a different answer"}])
    fake.replies.append(Reply(text="done"))
    h.controller.open()
    settle(h)
    assert h.store.journal_get(h.scope, session.id, "c1").state == JOURNAL_DONE  # the call was never touched: it is not unresolved
    assert {m["tool_call_id"]: m["content"][0]["text"] for m in tool_messages(session)} == {
        "c1": "someone answered this already", "c2": "ran bash U"}


def test_a_conflicting_post_is_ignored_and_the_call_is_answered_again(make, fake):
    session = awaiting_session(fake, [call("c1", tag="T")])
    h = make(session, execute=Stub())
    original = h.client.submit
    conflicts = []

    def conflicts_once(session_id, messages, **kw):
        if not conflicts:
            conflicts.append(1)
            from etcode_desktop.core.errors import Conflict
            raise Conflict(409, "unknown_tool_call", "Tool call is not awaiting a result")
        return original(session_id, messages, **kw)

    h.client.submit = conflicts_once
    fake.replies.append(Reply(text="done"))
    h.controller.open()
    settle(h)
    assert conflicts == [1] and not h.state.notices  # the 409 is not an error for the user
    assert tool_messages(session)[0]["content"][0]["text"] == "ran bash T"
    assert h.store.journal_get(h.scope, session.id, "c1").state == JOURNAL_POSTED


def test_a_backend_that_keeps_waiting_is_not_asked_at_the_speed_of_the_network(make, fake):
    session = awaiting_session(fake, [call("c1", tag="T")])
    h = make(session, execute=Stub(), poll_delays=(0.05, 0.1))

    def always_conflict(session_id, messages, **kw):
        from etcode_desktop.core.errors import Conflict
        raise Conflict(409, "unknown_tool_call", "Tool call is not awaiting a result")

    h.client.submit = always_conflict
    h.controller.open()
    time.sleep(1.0)
    asked = len(fake.requests_to("GET", f"/session/{session.id}$"))
    assert 3 <= asked <= 40, f"{asked} status requests in one second"  # waiting politely, not hammering


# --- an older backend ----------------------------------------------------------------------------------------------------------------------------

def test_a_backend_that_does_not_list_pending_calls_is_served_from_the_stream(legacy_fake, store, make_client, tmp_path):
    legacy_fake.auto_reply = True
    stub = Stub()
    h = Harness(legacy_fake, store, make_client, legacy_fake.add_session(), tools=ToolRunner(ToolPool(4), execute=stub, home=tmp_path))
    legacy_fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1", tag="A"), call("w", "websearch", query="q"), call("c2", tag="B")]),
                                Reply(text="done")])
    h.send("go")
    settle(h)
    assert sorted(stub.calls) == [("bash", "A"), ("bash", "B")]
    assert h.texts()[-1] == ("assistant", "done")
    h.controller.close()


def test_a_backend_that_does_not_list_pending_calls_is_served_from_the_history_after_a_restart(legacy_fake, store, make_client, tmp_path):
    legacy_fake.auto_reply = True
    stub = Stub()
    session = awaiting_session(legacy_fake, [call("c1", tag="A"), call("c2", tag="B")])
    h = Harness(legacy_fake, store, make_client, session, tools=ToolRunner(ToolPool(4), execute=stub, home=tmp_path))
    journal(h, "c1", "bash", {"tag": "A"}, JOURNAL_POSTED, [{"type": "text", "text": "kept A"}])
    session.pending[0]["result"] = [{"type": "text", "text": "kept A"}]  # the backend has it, as the journal says
    legacy_fake.replies.append(Reply(text="done"))
    h.controller.open()
    settle(h)
    assert stub.calls == [("bash", "B")]  # c1 was answered before (the journal says so)
    h.controller.close()


def test_calls_without_results_from_a_history():
    from etcode_desktop.core.models import HistoryEntry
    assistant = lambda calls: {"role": "assistant", "text": {"type": "text", "text": ""}, "tool_calls": calls}  # noqa: E731
    calls = [{"id": "a", "name": "bash", "arguments": {"command": "x"}}, {"id": "b", "name": "read_file", "arguments": {}},
             {"id": "w", "name": "websearch", "arguments": {}}]
    history = [HistoryEntry(1, {"role": "user", "content": []}), HistoryEntry(2, assistant([{"id": "old", "name": "bash", "arguments": {}}])),
               HistoryEntry(3, {"role": "tool", "tool_call_id": "old", "content": []}), HistoryEntry(4, assistant(calls)),
               HistoryEntry(5, {"role": "tool", "tool_call_id": "a", "content": []})]
    found = c._calls_without_results(history)
    assert list(found) == ["b"] and found["b"] == PendingToolCall("b", "read_file", {}, False)
    assert c._calls_without_results([]) == {}
    assert c._calls_without_results(history[:3]) == {}


# --- no runner ------------------------------------------------------------------------------------------------------------------------------------

def test_without_a_runner_the_calls_are_only_reported(fake, store, make_client):
    fake.auto_reply = True
    h = Harness(fake, store, make_client, fake.add_session())
    fake.replies.append(Reply(text="x", start_delay=0.1, tool_calls=[call("c1", tag="T")]))
    h.send("go")
    assert h.wait(lambda: h.of(c.ToolsPending))
    assert [p.id for p in h.of(c.ToolsPending)[0].calls] == ["c1"] and not h.of(c.ToolStarted)
    h.controller.close()
