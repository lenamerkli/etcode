"""The client against the REAL etcode_backend (Flask app + worker), with a fake LLM upstream.

The fake backend in fake_backend.py is only useful as long as it behaves like the real one,
so these tests run the same client calls against the real code. They are skipped if the
backend package or its dependencies are not available.

They don't test the backend itself, only what the desktop client relies on.
"""
import time

import pytest

from real_backend import RealBackend, Upstream  # noqa: E402  (skips this module if the backend is not available)

from etcode_desktop.core.errors import AuthError, BackendError, Conflict, NotFound  # noqa: E402
from etcode_desktop.core.models import text_part, tool_message, user_message  # noqa: E402
from etcode_desktop.core.sse import OUTCOME_DONE, OUTCOME_NO_GENERATION, Backoff, EventFollower  # noqa: E402
from fake_backend import PNG  # noqa: E402

FAST = Backoff(initial=0.01, maximum=0.05, give_up_after=10.0)


@pytest.fixture
def real(tmp_path):
    backend = RealBackend(tmp_path)
    yield backend
    backend.stop()


@pytest.fixture
def client(real, make_client):
    return make_client(real)


@pytest.fixture
def model(client):
    available = [m for m in client.models() if m.available]
    assert available, "the real backend reports no available model"
    return available[0].tech_name


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def say(client, session_id, text="hi", **kwargs):
    result = client.submit(session_id, [user_message(text)], **kwargs)
    assert result.generation_id, "sending a message to an idle session must start a generation"
    return result


def follow(client, session_id, **kwargs):
    follower = EventFollower(client, session_id, backoff=FAST, **kwargs)
    return follower, list(follower)


# --- catalog and errors ------------------------------------------------------------------------------

def test_models_have_the_fields_the_client_reads(client):
    models = client.models()
    assert models and all(m.tech_name and m.name and m.context_length > 0 for m in models)


def test_wrong_token_and_foreign_sessions(real, make_client, model):
    with pytest.raises(AuthError) as info:
        make_client(real, token="nope").models()
    assert info.value.code == "unauthorized"
    session = make_client(real).create_session(model)
    with pytest.raises(NotFound) as other:
        make_client(real, token="tok-b").get_session(session.id)
    assert other.value.code == "session_not_found"
    with pytest.raises(NotFound):
        make_client(real).get_session("0" * 32)


def test_unknown_model_is_a_400(client):
    with pytest.raises(BackendError) as info:
        client.create_session("no-such-model")
    assert (info.value.status, info.value.code) == (400, "unknown_model")


# --- a plain text generation ---------------------------------------------------------------------------

def test_text_generation_end_to_end(real, client, model):
    real.upstream.script = [Upstream.text(["Hel", "lo!"])]
    session = client.create_session(model, temperature=0.3)
    assert session.status == "idle" and session.options.get("temperature") == 0.3

    result = say(client, session.id, "hi there")
    assert result.accepted[0]["queued"] is False and result.accepted[0]["index"] == 1
    follower, events = follow(client, session.id)

    kinds = [e.kind for e in events]
    assert kinds[-1] == "done" and "text_delta" in kinds and "message_complete" in kinds and "usage" in kinds
    assert follower.outcome == OUTCOME_DONE
    assert [e.sequence for e in events] == list(range(len(events)))
    assert len({e.generation_id for e in events}) == 1 and events[0].generation_id == result.generation_id
    assert "".join(e.data["text"] for e in events if e.kind == "text_delta") == "Hello!"
    complete = next(e for e in events if e.kind == "message_complete")
    assert complete.data["index"] == 2 and complete.data["message"]["text"]["text"] == "Hello!"
    assert events[-1].data["status"] == "complete" and events[-1].next_generation_id is None
    usage = next(e for e in events if e.kind == "usage")
    assert usage.data["currency"] == "USD"

    wait_for(lambda: client.get_session(session.id).status == "idle")
    meta = client.get_session(session.id)
    assert meta.message_count == 3 and meta.cost.total == pytest.approx(0.0042) and meta.cost.total_exact == "0.0042"
    history = client.messages(session.id)
    assert [e.index for e in history] == [0, 1, 2]
    assert history[0].message["role"] == "system"  # the client skips this one when rendering
    assert history[1].message["content"] == [text_part("hi there")]
    assert history[2].message["text"]["text"] == "Hello!" and history[2].message["status"] == "complete"
    assert [e.index for e in client.last(session.id, 1)] == [2]
    assert real.upstream.requests[0]["temperature"] == 0.3


def test_stream_is_204_when_nothing_runs(client, model):
    session = client.create_session(model)
    follower, events = follow(client, session.id)
    assert events == [] and follower.outcome == OUTCOME_NO_GENERATION


def test_cancel_with_nothing_to_cancel(client, model):
    session = client.create_session(model)
    with pytest.raises(Conflict) as info:
        client.cancel(session.id)
    assert info.value.code == "no_generation"


def test_a_message_sent_while_generating_is_queued(real, client, model):
    real.upstream.script = [Upstream.text(["a", "b", "c", "d"], delay=0.15), Upstream.text("second", delay=0.3)]
    session = client.create_session(model)
    say(client, session.id, "first")
    wait_for(lambda: real.upstream.requests)
    queued = client.submit(session.id, [user_message("second")])
    assert queued.queued and "index" not in queued.accepted[0] and queued.session.queued_count == 1
    follower, events = follow(client, session.id)
    assert events[-1].kind == "done" and events[-1].next_generation_id  # the queued message gets its own generation
    assert events[-1].next_generation_id == client.get_session(session.id).active_generation_id
    _, events2 = follow(client, session.id)
    assert "".join(e.data["text"] for e in events2 if e.kind == "text_delta") == "second"
    assert events2[0].generation_id == events[-1].next_generation_id


def test_cancelling_a_running_generation(real, client, model):
    real.upstream.script = [Upstream.text(["x"] * 40, delay=0.1)]
    session = client.create_session(model)
    say(client, session.id, "go")
    follower = EventFollower(client, session.id, backoff=FAST)
    iterator = iter(follower)
    first = next(iterator)
    assert first.kind in ("text_delta", "reasoning_delta")
    assert client.cancel(session.id)["cancelled"] == "generation"
    rest = list(iterator)
    kinds = [e.kind for e in rest]
    assert "cancelled" in kinds and kinds[-1] == "done" and rest[-1].data["status"] == "cancelled"
    wait_for(lambda: client.get_session(session.id).status == "idle")
    assistant = client.last(session.id, 1)[0].message
    assert assistant["status"] == "cancelled" and assistant["text"]["text"].startswith("x")


# --- tool calls ------------------------------------------------------------------------------------------------

def start_tool_round(real, client, model, calls):
    real.upstream.script = [Upstream.tools(calls), Upstream.text("all done")]
    session = client.create_session(model)
    say(client, session.id, "do it")
    follower, events = follow(client, session.id)
    wait_for(lambda: client.get_session(session.id).status == "awaiting_tools")
    return session, events


def test_tool_call_round_trip(real, client, model):
    session, events = start_tool_round(real, client, model, [("call_a", "bash", {"command": "pwd"}), ("call_b", "read_file", {"path": "/etc/hostname"})])
    calls = [e for e in events if e.kind == "tool_call"]
    assert [(e.data["tool_call_id"], e.data["name"], e.data["arguments"]) for e in calls] == [
        ("call_a", "bash", {"command": "pwd"}), ("call_b", "read_file", {"path": "/etc/hostname"})]
    complete = next(e for e in events if e.kind == "message_complete")
    assert [c["id"] for c in complete.data["message"]["tool_calls"]] == ["call_a", "call_b"]
    assert events[-1].kind == "done" and events[-1].data["status"] == "tool_calls"

    meta = client.get_session(session.id)
    assert meta.status == "awaiting_tools"
    assert [(c.id, c.name, c.arguments, c.resolved) for c in meta.pending_tool_calls] == [
        ("call_a", "bash", {"command": "pwd"}, False), ("call_b", "read_file", {"path": "/etc/hostname"}, False)]
    assert [c.id for c in meta.unresolved_client_calls()] == ["call_a", "call_b"]

    # results may arrive in any order, one by one, and a repeat is accepted as a duplicate
    out_b = [text_part("host\n")]
    first = client.submit(session.id, [tool_message("call_b", out_b)])
    assert first.accepted == ({"tool_call_id": "call_b", "accepted": True},)
    assert client.get_session(session.id).status == "awaiting_tools"  # call_a is still missing
    assert client.submit(session.id, [tool_message("call_b", out_b)]).accepted[0].get("duplicate")
    with pytest.raises(Conflict) as differ:
        client.submit(session.id, [tool_message("call_b", [text_part("different")])])
    assert differ.value.code == "duplicate_tool_result"
    assert {c.id: c.resolved for c in client.get_session(session.id).pending_tool_calls} == {"call_a": False, "call_b": True}
    assert [c.id for c in first.session.unresolved_client_calls()] == ["call_a"]  # the submit response says so too

    last = client.submit(session.id, [tool_message("call_a", [text_part("/home/agent")])])
    assert last.accepted == ({"tool_call_id": "call_a", "accepted": True},)
    wait_for(lambda: client.get_session(session.id).status == "idle")
    assert client.get_session(session.id).pending_tool_calls == ()  # reported, and empty: not "unknown"
    history = [e.message for e in client.messages(session.id)]
    assert [m["role"] for m in history] == ["system", "user", "assistant", "tool", "tool", "assistant"]
    assert [m["tool_call_id"] for m in history if m["role"] == "tool"] == ["call_a", "call_b"]  # in call order
    assert history[-1]["text"]["text"] == "all done"
    with pytest.raises(Conflict) as late:  # the barrier is gone now
        client.submit(session.id, [tool_message("call_a", [text_part("/home/agent")])])
    assert late.value.code == "unknown_tool_call"


def test_websearch_results_cannot_be_posted_by_the_client(real, client, model):
    real.upstream.script = [Upstream.tools([("call_w", "websearch", {"query": "x"}), ("call_a", "bash", {"command": "ls"})]), Upstream.text("ok")]
    session = client.create_session(model, websearch=True)
    say(client, session.id, "search")
    follow(client, session.id)
    wait_for(lambda: client.get_session(session.id).status == "awaiting_tools")
    with pytest.raises(Conflict) as info:
        client.submit(session.id, [tool_message("call_w", [text_part("mine")])])
    assert info.value.code == "backend_tool"


def test_cancel_while_waiting_for_tools(real, client, model):
    session, _ = start_tool_round(real, client, model, [("call_a", "bash", {"command": "sleep 100"})])
    assert client.cancel(session.id)["cancelled"] == "tool_calls"
    wait_for(lambda: client.get_session(session.id).status == "idle")
    tool_message_text = [e.message for e in client.messages(session.id) if e.message["role"] == "tool"][0]["content"][0]["text"]
    assert "cancelled" in tool_message_text.lower()


# --- reconnecting against the real stream --------------------------------------------------------------------------

def test_follower_resumes_with_last_event_id_against_the_real_stream(real, client, model):
    parts = [f"p{i}," for i in range(25)]
    real.upstream.script = [Upstream.text(parts, delay=0.03)]
    session = client.create_session(model)
    say(client, session.id, "stream please")
    follower = EventFollower(client, session.id, backoff=FAST)
    events, dropped = [], 0
    for event in follower:
        events.append(event)
        if len(events) in (3, 8):  # break the connection under the follower twice
            follower._connection.close()
            dropped += 1
    assert dropped == 2 and events[-1].kind == "done"
    sequences = [e.sequence for e in events]
    assert sequences == list(range(len(events))), "events must be complete, in order and unique"
    text = "".join(e.data["text"] for e in events if e.kind == "text_delta")
    assert text.replace(",", "") == "".join(p.rstrip(",") for p in parts)


def test_replay_from_the_start_when_connecting_late(real, client, model):
    real.upstream.script = [Upstream.text(["one", "two", "three"], delay=0.1)]
    session = client.create_session(model)
    say(client, session.id, "go")
    wait_for(lambda: real.upstream.requests)
    time.sleep(0.15)  # some events exist already
    _, events = follow(client, session.id)
    assert events[0].sequence == 0, "a late listener is first replayed what it missed"
    assert "".join(e.data["text"] for e in events if e.kind == "text_delta") == "onetwothree"


# --- sessions list ---------------------------------------------------------------------------------------------------

def test_list_sessions(real, client, make_client, model):
    first = client.create_session(model)
    second = client.create_session(model)
    foreign = make_client(real, token="tok-b").create_session(model)
    real.upstream.script = [Upstream.text("hello")]
    time.sleep(0.01)
    say(client, first.id, "  Fix the\n failing test  ")
    wait_for(lambda: client.get_session(first.id).status == "idle")

    listing = client.list_sessions()
    assert listing.total == 2
    assert [s.id for s in listing.sessions] == [first.id, second.id]  # the active one first
    assert foreign.id not in [s.id for s in listing.sessions]
    assert listing.sessions[0].title == "Fix the failing test" and listing.sessions[1].title is None
    assert listing.sessions[0].message_count == 3 and listing.sessions[0].status == "idle"
    assert listing.sessions[0].updated_at >= listing.sessions[0].created_at > 0
    assert listing.sessions[0].pending_tool_calls == ()

    assert [s.id for s in client.list_sessions(limit=1).sessions] == [first.id]
    page = client.list_sessions(limit=1, offset=1)
    assert [s.id for s in page.sessions] == [second.id] and page.total == 2
    assert make_client(real, token="tok-b").list_sessions().total == 1


def test_list_sessions_paging_errors(client):
    for kwargs in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        with pytest.raises(BackendError) as info:
            client.list_sessions(**kwargs)
        assert (info.value.status, info.value.code) == (400, "invalid_number")


def test_list_shows_sessions_that_wait_for_tools(real, client, model):
    session, _ = start_tool_round(real, client, model, [("call_a", "bash", {"command": "ls"})])
    entry = client.list_sessions().sessions[0]
    assert entry.id == session.id and entry.status == "awaiting_tools"
    assert [c.id for c in entry.unresolved_client_calls()] == ["call_a"]


# --- images ---------------------------------------------------------------------------------------------------------

def test_image_round_trip_and_use_in_a_message(real, client, model):
    available = {m.tech_name: m for m in client.models()}
    if not available[model].supports_images:
        pytest.skip("the first available model has no image support")
    uploaded = client.upload_image(PNG, "x.png")
    assert uploaded.mime_type == "image/png" and uploaded.media_type == "image"
    assert client.download_image(uploaded.hash).data == PNG
    real.upstream.script = [Upstream.text("nice picture")]
    session = client.create_session(model)
    client.submit(session.id, [user_message("what is this?", [uploaded.hash])])
    follow(client, session.id)
    wait_for(lambda: client.get_session(session.id).status == "idle")
    content = client.messages(session.id)[1].message["content"]
    assert content == [text_part("what is this?"), {"type": "image", "hash": uploaded.hash}]
    with pytest.raises(BackendError) as bad:
        client.upload_image(b"not an image at all")
    assert bad.value.status == 415 and bad.value.code == "unsupported_media"
    with pytest.raises(BackendError) as missing:
        client.submit(session.id, [user_message("x", ["f" * 64])])  # a hash that was never uploaded
    assert missing.value.status in (400, 404, 422)
