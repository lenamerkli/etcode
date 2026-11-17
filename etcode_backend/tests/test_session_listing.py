"""GET /sessions and the pending_tool_calls field of the session metadata."""
import time

import pytest

from conftest import A, B, FakeUpstream, new_session, say, wait_for, wait_status
from etcode_backend.db import connect, transaction


def listing(client, headers=A, query=""):
    return client.get("/sessions" + query, headers=headers)


def touch(settings, sid, updated_at):
    """Set a session's last activity, so that the tests don't depend on the clock."""
    with connect(settings) as con, transaction(con):
        con.execute("UPDATE sessions SET updated_at=? WHERE id=?", (updated_at, sid))


def post_result(client, sid, call_id, text, headers=A):
    return client.post(f"/session/{sid}/message", json={"role": "tool", "tool_call_id": call_id, "content": [{"type": "text", "text": text}]}, headers=headers)


# --- GET /sessions -----------------------------------------------------------------------------

def test_requires_authentication(client):
    assert client.get("/sessions").status_code == 401
    assert client.get("/sessions", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_empty_list(client):
    r = listing(client)
    assert r.status_code == 200 and r.json == {"sessions": [], "total": 0}


def test_lists_only_the_callers_sessions(client):
    mine = [new_session(client, headers=A) for _ in range(2)]
    theirs = new_session(client, headers=B)
    a, b = listing(client, A).json, listing(client, B).json
    assert {s["id"] for s in a["sessions"]} == set(mine) and a["total"] == 2
    assert [s["id"] for s in b["sessions"]] == [theirs] and b["total"] == 1


def test_entries_have_the_session_metadata_and_a_title(client):
    sid = new_session(client, websearch=True, temperature=0.4)
    entry = listing(client).json["sessions"][0]
    meta = client.get(f"/session/{sid}", headers=A).json
    assert entry == {**meta, "title": None}
    for key in ("id", "model", "websearch", "options", "status", "message_count", "queued_count",
                "active_generation_id", "cost", "pending_tool_calls", "created_at", "updated_at"):
        assert key in entry
    assert isinstance(entry["created_at"], str) and "T" in entry["created_at"]  # ISO 8601, like GET /session/<id>


def test_most_recently_active_first_with_the_id_as_tie_breaker(client, settings):
    first, second, third = (new_session(client) for _ in range(3))
    touch(settings, first, 300.0)
    touch(settings, second, 100.0)
    touch(settings, third, 200.0)
    assert [s["id"] for s in listing(client).json["sessions"]] == [first, third, second]
    touch(settings, second, 300.0)
    expected = sorted([first, second], reverse=True) + [third]
    assert [s["id"] for s in listing(client).json["sessions"]] == expected


def test_activity_moves_a_session_to_the_front(client, worker, upstream):
    old, new = new_session(client), new_session(client)
    assert [s["id"] for s in listing(client).json["sessions"]] == [new, old]
    time.sleep(0.01)
    say(client, old, "wake up")
    wait_status(client, old, "idle")
    assert [s["id"] for s in listing(client).json["sessions"]] == [old, new]


def test_pagination(client, settings):
    ids = [new_session(client) for _ in range(5)]
    for position, sid in enumerate(ids):
        touch(settings, sid, 1000.0 + position)  # ids[4] is the newest
    newest_first = list(reversed(ids))
    page = listing(client, query="?limit=2").json
    assert [s["id"] for s in page["sessions"]] == newest_first[:2] and page["total"] == 5
    page = listing(client, query="?limit=2&offset=2").json
    assert [s["id"] for s in page["sessions"]] == newest_first[2:4] and page["total"] == 5
    page = listing(client, query="?limit=2&offset=4").json
    assert [s["id"] for s in page["sessions"]] == newest_first[4:] and page["total"] == 5
    assert listing(client, query="?offset=99").json == {"sessions": [], "total": 5}
    assert len(listing(client).json["sessions"]) == 5  # the default limit is large enough


@pytest.mark.parametrize("query", ["?limit=0", "?limit=501", "?limit=-1", "?limit=abc", "?limit=", "?offset=-1",
                                   "?offset=abc", "?offset=1000001", "?limit=1.5"])
def test_invalid_paging_parameters(client, query):
    new_session(client)
    r = listing(client, query=query)
    assert r.status_code == 400 and r.json["error"]["code"] == "invalid_number"


def test_limit_bounds_are_accepted(client):
    new_session(client)
    assert listing(client, query="?limit=1").status_code == 200
    assert listing(client, query="?limit=500&offset=1000000").status_code == 200


def test_title_is_the_first_user_message(client):
    sid = new_session(client)
    say(client, sid, "  Fix the\n  failing   test\tplease  ")
    assert listing(client).json["sessions"][0]["title"] == "Fix the failing test please"


def test_title_is_cut_at_80_characters(client):
    sid = new_session(client)
    say(client, sid, "x" * 200)
    title = listing(client).json["sessions"][0]["title"]
    assert title == "x" * 80


def test_title_skips_an_image_only_message_and_ignores_the_system_prompt(client):
    import io
    from conftest import PNG
    upload = client.post("/media/images", data={"file": (io.BytesIO(PNG), "a.png")}, headers=A, content_type="multipart/form-data")
    image = {"type": "image", "hash": upload.json["hash"]}
    sid = new_session(client)
    r = client.post(f"/session/{sid}/message", json={"role": "user", "content": [image]}, headers=A)
    assert r.status_code == 202
    assert listing(client).json["sessions"][0]["title"] is None  # no text yet (and not the system prompt)


def test_title_ignores_queued_messages(client, worker, upstream):
    def slow(handler, body):
        FakeUpstream.sse(handler, [{"choices": [{"delta": {"content": "x"}}]} for _ in range(20)]
                         + [{"choices": [{"delta": {}, "finish_reason": "stop"}]}], delay=0.05)
    upstream.script = [slow]
    sid = new_session(client)
    say(client, sid, "first question")
    wait_for(lambda: upstream.requests)
    queued = say(client, sid, "second question")
    assert queued["accepted"][0]["queued"] is True
    assert listing(client).json["sessions"][0]["title"] == "first question"


def test_list_reports_status_and_cost(client, worker, upstream):
    upstream.script = [FakeUpstream.text_reply("hello", usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.25})]
    sid = new_session(client)
    say(client, sid, "hi")
    wait_status(client, sid, "idle")
    entry = listing(client).json["sessions"][0]
    assert entry["status"] == "idle" and entry["message_count"] == 3
    assert entry["cost"]["total_exact"] == "0.25"


# --- pending_tool_calls ----------------------------------------------------------------------------

def start_tool_round(client, upstream, calls, followup="all done", **session_args):
    upstream.script = [FakeUpstream.tool_reply(calls), FakeUpstream.text_reply(followup)]
    sid = new_session(client, **session_args)
    say(client, sid, "do it")
    wait_status(client, sid, "awaiting_tools")
    return sid


def pending(client, sid):
    return client.get(f"/session/{sid}", headers=A).json["pending_tool_calls"]


def test_no_pending_calls_for_a_new_session(client):
    sid = new_session(client)
    assert client.get(f"/session/{sid}", headers=A).json["pending_tool_calls"] == []


def test_pending_calls_through_a_whole_tool_round(client, worker, upstream):
    sid = start_tool_round(client, upstream, [("call_a", "bash", {"command": "pwd"}), ("call_b", "read_file", {"path": "x", "start_line": 3})])
    assert pending(client, sid) == [
        {"id": "call_a", "name": "bash", "arguments": {"command": "pwd"}, "resolved": False, "backend": False},
        {"id": "call_b", "name": "read_file", "arguments": {"path": "x", "start_line": 3}, "resolved": False, "backend": False},
    ]
    # a result arriving first (out of order) marks only that call as resolved
    response = post_result(client, sid, "call_b", "contents")
    assert response.status_code == 202
    assert [(c["id"], c["resolved"]) for c in pending(client, sid)] == [("call_a", False), ("call_b", True)]
    assert [(c["id"], c["resolved"]) for c in response.json["session"]["pending_tool_calls"]] == [("call_a", False), ("call_b", True)]
    assert client.get(f"/session/{sid}", headers=A).json["status"] == "awaiting_tools"
    # the stored result itself is never exposed
    assert "contents" not in str(pending(client, sid))
    # the last result clears the barrier and the list
    assert post_result(client, sid, "call_a", "/home/agent").status_code == 202
    wait_status(client, sid, "idle")
    assert pending(client, sid) == []


def test_pending_calls_keep_the_call_order(client, worker, upstream):
    calls = [(f"call_{i}", "bash", {"command": str(i)}) for i in range(5)]
    sid = start_tool_round(client, upstream, calls)
    for call_id in ("call_4", "call_1"):
        post_result(client, sid, call_id, "x")
    assert [(c["id"], c["resolved"]) for c in pending(client, sid)] == [
        ("call_0", False), ("call_1", True), ("call_2", False), ("call_3", False), ("call_4", True)]


def test_a_repeated_result_does_not_change_the_list(client, worker, upstream):
    sid = start_tool_round(client, upstream, [("call_a", "bash", {"command": "a"}), ("call_b", "bash", {"command": "b"})])
    post_result(client, sid, "call_a", "x")
    before = pending(client, sid)
    assert post_result(client, sid, "call_a", "x").json["accepted"][0].get("duplicate") is True
    assert pending(client, sid) == before


def test_websearch_calls_are_listed_as_backend_calls(client, worker, upstream):
    sid = start_tool_round(client, upstream, [("search_1", "websearch", {"query": "python"}), ("read_1", "read_file", {"path": "x"})],
                           websearch=True)
    assert [(c["id"], c["name"], c["resolved"], c["backend"]) for c in pending(client, sid)] == [
        ("search_1", "websearch", False, True), ("read_1", "read_file", False, False)]


def test_cancelling_the_tool_calls_clears_the_list(client, worker, upstream):
    sid = start_tool_round(client, upstream, [("call_a", "bash", {"command": "sleep 100"})])
    assert pending(client, sid)
    assert client.post(f"/session/{sid}/cancel", headers=A).status_code == 202
    wait_status(client, sid, "idle")
    assert pending(client, sid) == []


def test_list_entries_carry_pending_calls_too(client, worker, upstream):
    sid = start_tool_round(client, upstream, [("call_a", "bash", {"command": "ls"})])
    entry = listing(client).json["sessions"][0]
    assert entry["id"] == sid and entry["status"] == "awaiting_tools"
    assert [c["id"] for c in entry["pending_tool_calls"]] == ["call_a"]


def test_pending_calls_are_private_to_the_owner(client, worker, upstream):
    sid = start_tool_round(client, upstream, [("call_a", "bash", {"command": "ls"})])
    assert client.get(f"/session/{sid}", headers=B).status_code == 404
    assert listing(client, B).json["sessions"] == []
