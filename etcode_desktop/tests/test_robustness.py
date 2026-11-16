"""Outages, refusals and bugs: the controller must recover, and must never lose or repeat what matters."""
import sqlite3
import time

import pytest

pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from etcode_desktop.core import controller as c  # noqa: E402
from etcode_desktop.core.errors import Unreachable  # noqa: E402
from etcode_desktop.core.session_state import NOTICE_CONNECTION, NOTICE_ERROR, NOTICE_LOAD, NOTICE_UNAVAILABLE  # noqa: E402
from etcode_desktop.core.sse import Backoff  # noqa: E402
from etcode_desktop.core.store import JOURNAL_DONE, JOURNAL_POSTED, Scope, SessionStore  # noqa: E402
from etcode_desktop.core.tools.scheduler import ToolPool, ToolRunner  # noqa: E402
from fake_backend import Reply  # noqa: E402
from test_controller import Harness  # noqa: E402
from test_controller_tools import Stub, awaiting_session, call, journal, tool_messages  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "store.sqlite3")
    yield s
    s.close()


@pytest.fixture
def make(fake, store, make_client):
    created = []

    def factory(session=None, token="tok-a", **kwargs):
        fake.auto_reply = True
        h = Harness(fake, store, make_client, session or fake.add_session(), token=token, **kwargs)
        created.append(h)
        return h

    yield factory
    for h in created:
        h.controller.close()


# --- the history cannot be loaded ---------------------------------------------------------------------------------------------

def test_a_history_that_could_not_be_loaded_loads_when_recover_is_called(make, fake):
    session = fake.add_session()
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "hello"}]})
    h = make(session)
    fake.go_down()
    h.controller.open()
    assert h.wait(lambda: NOTICE_LOAD in h.state.notices)
    fake.come_back()
    h.controller.recover()
    assert h.wait(lambda: h.state.loaded and NOTICE_LOAD not in h.state.notices)
    assert h.texts() == [("user", "hello")]


def test_recover_does_nothing_for_a_session_that_is_fine(make, fake):
    h = make()
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    before = len(fake.requests_to("GET", "/messages$"))
    h.controller.recover()
    time.sleep(0.2)
    assert len(fake.requests_to("GET", "/messages$")) == before  # not loaded again


def test_a_refused_token_is_recovered_when_it_is_accepted_again(make, fake):
    session = fake.add_session()
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "hello"}]})
    h = make(session)
    fake.tokens.discard("tok-a")
    h.controller.open()
    assert h.wait(lambda: NOTICE_UNAVAILABLE in h.state.notices)
    assert not h.state.notices[NOTICE_UNAVAILABLE].missing and not h.state.can_send
    assert not h.store.get(h.scope, session.id).is_missing  # a refused token says nothing about the session
    fake.tokens.add("tok-a")
    h.controller.recover()
    assert h.wait(lambda: h.state.loaded and NOTICE_UNAVAILABLE not in h.state.notices)
    assert h.state.can_send and h.texts() == [("user", "hello")]


def test_a_session_that_comes_back_is_recovered_too(make, fake):
    session = fake.add_session()
    h = make(session)
    del fake.sessions[session.id]
    h.controller.open()
    assert h.wait(lambda: NOTICE_UNAVAILABLE in h.state.notices and h.state.notices[NOTICE_UNAVAILABLE].missing)
    assert h.store.get(h.scope, session.id).is_missing
    fake.sessions[session.id] = session
    h.controller.recover()
    assert h.wait(lambda: h.state.loaded and NOTICE_UNAVAILABLE not in h.state.notices)
    assert not h.store.get(h.scope, session.id).is_missing


def test_a_session_that_was_refused_in_the_middle_of_a_conversation_recovers(make, fake):
    h = make()
    fake.replies.extend([Reply(text="first"), Reply(text="second")])
    h.send("one")
    h.wait_idle()
    fake.tokens.discard("tok-a")
    h.send("two")
    assert h.wait(lambda: any(isinstance(e, c.UserFailed) for e in h.events))
    assert h.wait(lambda: NOTICE_UNAVAILABLE in h.state.notices)
    fake.tokens.add("tok-a")
    h.controller.recover()
    assert h.wait(lambda: h.state.can_send and NOTICE_UNAVAILABLE not in h.state.notices)
    assert [t for r, t in h.texts() if r == "assistant"] == ["first"]  # the refused message is shown as it is: not sent


# --- the connection drops and comes back ----------------------------------------------------------------------------------------

def test_a_backend_that_restarts_in_the_middle_of_an_answer_gives_the_whole_answer_once(make, fake):
    text = "".join(f"{i}," for i in range(30))
    h = make(backoff=Backoff(initial=0.02, factor=1.5, maximum=0.1, give_up_after=10))
    fake.replies.append(Reply(text=text, chunks=30, delay=0.05, start_delay=0.1))
    h.send("go")
    assert h.wait(lambda: len(h.of(c.LiveText)) >= 4)
    fake.go_down()
    assert h.wait(lambda: h.of(c.ConnectionLost))
    time.sleep(0.4)
    fake.come_back()
    h.wait_idle(15)
    assert h.texts()[-1] == ("assistant", text)
    assert h.of(c.ConnectionRestored) and NOTICE_CONNECTION not in h.state.notices


def test_retry_now_ends_the_wait_between_attempts(make, fake):
    text = "".join(f"{i}," for i in range(20))
    h = make(backoff=Backoff(initial=0.01, maximum=0.02, give_up_after=0.1), retry_delays=(30.0,))
    fake.replies.append(Reply(text=text, chunks=20, delay=0.05, start_delay=0.1))
    h.send("go")
    assert h.wait(lambda: len(h.of(c.LiveText)) >= 3)
    fake.go_down()
    assert h.wait(lambda: any(e.retry_in == 30.0 for e in h.of(c.ConnectionLost)), 10)  # the follower gave up: 30 s until the next try
    fake.come_back()
    started = time.monotonic()
    h.controller.retry_now()
    h.wait_idle(15)
    assert time.monotonic() - started < 10 and h.texts()[-1] == ("assistant", text)


def test_a_message_sent_while_the_backend_is_down_is_not_sent_and_can_be_sent_again(make, fake):
    h = make(verify_grace=0.05, retry_delays=(0.05,))
    fake.replies.append(Reply(text="answer"))
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    fake.go_down()
    key = h.send("hello?")
    assert h.wait(lambda: h.state.model.get(key).state == "unknown")
    time.sleep(0.3)
    assert h.state.model.get(key).state == "unknown"  # still unknown: the backend cannot be asked
    fake.come_back()
    assert h.wait(lambda: h.state.model.get(key).state == "failed", 10)  # now it is known: the message never arrived
    assert h.state.model.get(key).error == "the backend did not receive it"
    assert fake.sessions[h.session.id].messages[-1]["role"] == "system"


def test_an_unknown_message_that_did_arrive_is_shown_once(make, fake):
    h = make(verify_grace=0.05)
    fake.replies.append(Reply(text="answer"))
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    original = h.client.submit

    def lose_the_answer(session_id, messages, **kw):
        original(session_id, messages, **kw)  # the backend gets the message...
        raise Unreachable("the answer was lost")  # ...and the client never hears about it

    h.client.submit = lose_the_answer
    key = h.send("did it arrive?")
    h.wait_idle(15)
    assert h.state.model.get(key) is None  # replaced by the committed copy
    assert h.texts() == [("user", "did it arrive?"), ("assistant", "answer")]


def test_verification_gives_up_quietly_when_the_session_is_gone(make, fake):
    h = make(verify_grace=0.05)
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    fake.go_down()
    key = h.send("hello")
    assert h.wait(lambda: h.state.model.get(key).state == "unknown")
    del fake.sessions[h.session.id]
    fake.come_back()
    assert h.wait(lambda: NOTICE_UNAVAILABLE in h.state.notices or h.of(c.Unavailable), 10)
    time.sleep(0.3)
    assert h.state.model.get(key).state == "unknown"  # nothing was claimed about a message that cannot be checked


def test_verification_does_not_wait_forever(make, fake):
    h = make(verify_grace=0.01, verify_attempts=3, retry_delays=(0.02,))
    h.controller.open()
    assert h.wait(lambda: h.state.loaded)
    fake.go_down()
    key = h.send("hello")
    assert h.wait(lambda: h.state.model.get(key).state == "unknown")
    time.sleep(0.5)
    asked = len(fake.requests)
    time.sleep(0.5)
    assert len(fake.requests) == asked  # it gave up after its attempts (nothing arrives while the backend is down anyway)
    assert h.state.model.get(key).state == "unknown"


# --- bugs and disk trouble ---------------------------------------------------------------------------------------------------------------

def test_an_unexpected_error_in_the_follower_is_survived(make, fake):
    h = make(retry_delays=(0.02,))
    fake.replies.append(Reply(text="all fine", start_delay=0.1))
    original = h.controller._follow_once
    failures = []

    def sometimes_broken():
        if len(failures) < 2:
            failures.append(1)
            raise RuntimeError("a bug")
        return original()

    h.controller._follow_once = sometimes_broken
    h.send("go")
    h.wait_idle(10)
    assert failures == [1, 1] and h.texts()[-1] == ("assistant", "all fine")
    assert not h.state.notices


def test_the_follower_gives_up_after_repeated_unexpected_errors_and_says_so(make, fake):
    h = make(retry_delays=(0.01,))

    def always_broken():
        raise RuntimeError("a persistent bug")

    h.controller._follow_once = always_broken
    h.controller._ensure_following()
    assert h.wait(lambda: NOTICE_ERROR in h.state.notices)
    assert "unexpected error: RuntimeError: a persistent bug" in h.state.notices[NOTICE_ERROR].text
    assert h.wait(lambda: not h.controller.is_following)


def test_a_failure_of_one_round_does_not_count_against_the_next(make, fake):
    h = make(retry_delays=(0.01,))
    fake.replies.append(Reply(text="ok", start_delay=0.1))
    original = h.controller._follow_once
    calls = []

    def pattern():
        calls.append(1)
        if len(calls) in (1, 2, 4, 5):  # two failures, a success, two failures: never three in a row
            raise RuntimeError("bug")
        return original() if len(calls) != 3 else True

    h.controller._follow_once = pattern
    h.send("go")
    h.wait_idle(10)
    assert NOTICE_ERROR not in h.state.notices


# --- the journal ------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def make_tools(fake, store, make_client, tmp_path):
    created = []

    def factory(session=None, execute=None, token="tok-a", **kwargs):
        fake.auto_reply = True
        runner = ToolRunner(ToolPool(4), execute=execute or Stub(), home=tmp_path)
        h = Harness(fake, store, make_client, session or fake.add_session(), token=token, tools=runner, **kwargs)
        created.append(h)
        return h

    yield factory
    for h in created:
        h.controller.close()


def test_a_journal_that_cannot_be_written_means_the_command_is_not_run(make_tools, fake, store):
    stub = Stub()
    h = make_tools(execute=stub)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    store.journal_start = broken
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1", tag="T")]), Reply(text="I will not rely on that.")])
    h.send("go")
    h.wait_idle(10)
    assert stub.calls == []  # nothing was run without a record of it
    text = tool_messages(h.session)[0]["content"][0]["text"]
    assert text.startswith("Error: the desktop client could not write its journal, so the command was not run")
    assert h.texts()[-1] == ("assistant", "I will not rely on that.")  # and the session is not left waiting


def test_a_journal_that_fails_after_the_command_still_posts_the_result(make_tools, fake, store):
    stub = Stub()
    h = make_tools(execute=stub)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    store.journal_done = broken
    store.journal_posted = broken
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1", tag="T")]), Reply(text="done")])
    h.send("go")
    h.wait_idle(10)
    assert stub.calls == [("bash", "T")] and tool_messages(h.session)[0]["content"][0]["text"] == "ran bash T"


def test_a_changed_token_does_not_make_a_command_run_again(make_tools, fake, store, make_client):
    stub = Stub()
    session = awaiting_session(fake, [call("c1", tag="T")])
    fake.tokens.add("tok-b")
    session.owner = "tok-b"  # the same session, reached with the token that replaced the old one
    h = make_tools(session, execute=stub, token="tok-b")
    old_scope = Scope.for_token(fake.url, "tok-a")
    store.journal_start(old_scope, session.id, "c1", "bash", {"tag": "T"})
    store.journal_done(old_scope, session.id, "c1", [{"type": "text", "text": "result from before the change"}])
    fake.replies.append(Reply(text="done"))
    h.controller.open()
    h.wait_idle(10)
    assert stub.calls == []
    assert tool_messages(session)[0]["content"][0]["text"] == "result from before the change"
    assert store.journal_get(Scope.for_token(fake.url, "tok-b"), session.id, "c1").state == JOURNAL_POSTED


def test_a_result_that_cannot_be_posted_because_the_backend_is_down_waits_and_the_command_runs_once(make_tools, fake):
    stub = Stub()
    h = make_tools(execute=stub, backoff=Backoff(initial=0.01, maximum=0.02, give_up_after=0.1), retry_delays=(0.05,))
    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[call("c1", tag="T", sleep=0.5)]), Reply(text="done")])
    h.send("go")
    assert h.wait(lambda: h.of(c.ToolStarted))
    fake.go_down()  # while the command runs
    assert h.wait(lambda: h.of(c.ToolFinished))
    time.sleep(0.5)  # the post cannot work
    assert h.store.journal_get(h.scope, h.session.id, "c1").state == JOURNAL_DONE
    fake.come_back()
    h.controller.retry_now()
    h.wait_idle(15)
    assert stub.calls == [("bash", "T")] and h.texts()[-1] == ("assistant", "done")
    assert h.store.journal_get(h.scope, h.session.id, "c1").state == JOURNAL_POSTED


def test_a_session_that_waits_for_tools_survives_a_backend_that_was_away(make_tools, fake):
    stub = Stub()
    session = awaiting_session(fake, [call("c1", tag="A"), call("c2", tag="B")])
    h = make_tools(session, execute=stub)
    journal(h, "c1", "bash", {"tag": "A"}, JOURNAL_DONE, [{"type": "text", "text": "kept"}])
    fake.go_down()
    fake.replies.append(Reply(text="done"))
    h.controller.open()
    assert h.wait(lambda: NOTICE_LOAD in h.state.notices)
    fake.come_back()
    h.controller.recover()
    h.wait_idle(15)
    assert stub.calls == [("bash", "B")]  # the one that was never started; the finished one is posted from the journal
    assert {m["tool_call_id"] for m in tool_messages(session)} == {"c1", "c2"}
