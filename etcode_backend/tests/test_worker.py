import io
import json
import threading
import time

from conftest import A, B, PNG, FakeUpstream, messages, new_session, say, wait_for, wait_status
from etcode_backend.db import connect, loads
from etcode_backend.search import SearchQuery
from etcode_backend.worker import CANCEL_NOTICE


def post_result(client, sid, call_id, content):
    return client.post(f"/session/{sid}/message", json={"role": "tool", "tool_call_id": call_id, "content": content}, headers=A)


def test_text_reasoning_sse_and_options(client, worker, upstream, settings):
    sid = new_session(client, model="deepseek-v4.1-flash-1017", reasoning_effort="high")
    upstream.script = [FakeUpstream.text_reply("hello!", reasoning="think")]
    result = say(client, sid, "hi", temperature=0.3, max_tokens=37)
    gid = result["generation_id"]
    assert gid
    wait_status(client, sid, "idle")
    req = upstream.requests[0]["body"]
    assert req["model"] == "deepseek/deepseek-v4.1-flash-1017:nitro"
    assert req["provider"] == {"order": ["together"], "allow_fallbacks": False}
    assert req["reasoning"] == {"effort": "high"}
    assert req["temperature"] == 0.3 and req["max_tokens"] == 37
    assert req["parallel_tool_calls"] is True
    hist = messages(client, sid)
    assert hist[-1]["message"]["text"]["text"] == "hello!"
    assert hist[-1]["message"]["reasoning"]["text"] == "think"
    with connect(settings) as con:
        events = con.execute("SELECT kind,payload FROM events WHERE generation_id=? ORDER BY seq", (gid,)).fetchall()
        assert [e["kind"] for e in events] == ["reasoning_delta", "text_delta", "usage", "message_complete", "done"]
        assert loads(events[-1]["payload"])["status"] == "complete"


def test_sse_replay_and_multiple_listeners(client, worker, upstream):
    def slow(handler, body):
        chunks = [{"choices": [{"delta": {"content": str(i)}}]} for i in range(10)]
        chunks += [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        FakeUpstream.sse(handler, chunks, delay=0.08)
    upstream.script = [slow]
    sid = new_session(client)
    say(client, sid, "hi")
    wait_for(lambda: upstream.requests)
    r1 = client.get(f"/session/{sid}/stream", headers=A, buffered=False)
    r2 = client.get(f"/session/{sid}/stream", headers=A, buffered=False)
    assert r1.status_code == 200 and r2.status_code == 200
    events1 = b"".join(r1.response).decode()
    events2 = b"".join(r2.response).decode()
    assert "event: done" in events1 and "event: done" in events2
    assert "event: text_delta" in events1 and "event: text_delta" in events2
    assert "event: message_complete" in events1
    assert client.get(f"/session/{sid}/stream", headers=A).status_code == 204


def test_tool_barrier_queue_and_image_workaround(client, worker, upstream):
    sid = new_session(client, model="deepseek-v4.1-flash-1017")
    upstream.script = [FakeUpstream.tool_reply([("call_a", "bash", {"command": "pwd"}),
                                                 ("call_b", "view_image", {"path": "/x.png"})]),
                       FakeUpstream.text_reply("image seen"), FakeUpstream.text_reply("queued answered")]
    say(client, sid, "look")
    wait_status(client, sid, "awaiting_tools")
    # User message is queued until both results and follow-up assistant reply.
    queued = say(client, sid, "also answer me")
    assert queued["accepted"][0]["queued"]
    uploaded = client.post("/media/images", data={"file": (io.BytesIO(PNG), "x.png")}, headers=A, content_type="multipart/form-data").json["hash"]
    result = post_result(client, sid, "call_b", [{"type": "image", "hash": uploaded}])
    assert result.status_code == 202
    assert client.get(f"/session/{sid}", headers=A).json["status"] == "awaiting_tools"
    assert post_result(client, sid, "call_b", [{"type": "text", "text": "different"}]).status_code == 409
    result = post_result(client, sid, "call_a", [{"type": "text", "text": "/home/agent"}])
    assert result.status_code == 202 and result.json["generation_id"]
    wait_for(lambda: len(upstream.requests) >= 3)
    wait_status(client, sid, "idle")
    hist = messages(client, sid)
    assert [m["message"]["role"] for m in hist] == ["system", "user", "assistant", "tool", "tool", "assistant", "user", "assistant"]
    assert hist[3]["message"]["tool_call_id"] == "call_a"
    assert hist[4]["message"]["tool_call_id"] == "call_b"
    second = upstream.requests[1]["body"]["messages"]
    assert [m["role"] for m in second[-4:]] == ["assistant", "tool", "tool", "user"]
    assert second[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert hist[5]["message"]["text"]["text"] == "image seen"
    assert hist[-1]["message"]["text"]["text"] == "queued answered"
    assert post_result(client, sid, "call_b", [{"type": "image", "hash": uploaded}]).status_code == 409


def test_websearch_only_auto_resumes(client, worker, upstream, monkeypatch):
    class Search:
        def search(self, query):
            assert query.query == "python"
            return [{"title": "Python", "url": "https://python.org", "description": "ok", "age": None}]
    monkeypatch.setattr("etcode_backend.worker.get_search_provider", lambda s: Search())
    upstream.script = [FakeUpstream.tool_reply([("search_1", "websearch", {"query": "python"})]),
                       FakeUpstream.text_reply("found it")]
    sid = new_session(client, websearch=True)
    say(client, sid, "search")
    wait_status(client, sid, "idle")
    hist = messages(client, sid)
    assert [m["message"]["role"] for m in hist] == ["system", "user", "assistant", "tool", "assistant"]
    assert "Python" in hist[3]["message"]["content"][0]["text"]
    assert len(upstream.requests) == 2


def test_mixed_websearch_waits_for_client(client, worker, upstream, monkeypatch):
    class Search:
        def search(self, query):
            return [{"title": "Y", "url": "https://example.com", "description": "", "age": None}]
    monkeypatch.setattr("etcode_backend.worker.get_search_provider", lambda s: Search())
    upstream.script = [FakeUpstream.tool_reply([("search_1", "websearch", {"query": "python"}),
                                                 ("read_1", "read_file", {"path": "x"})]),
                       FakeUpstream.text_reply("done")]
    sid = new_session(client, websearch=True)
    say(client, sid, "both")
    wait_status(client, sid, "awaiting_tools")
    assert len(upstream.requests) == 1
    assert post_result(client, sid, "search_1", [{"type": "text", "text": "hijack"}]).status_code == 409
    assert post_result(client, sid, "read_1", [{"type": "text", "text": "file"}]).status_code == 202
    wait_status(client, sid, "idle")
    assert len(upstream.requests) == 2


def test_cancel_during_stream(client, worker, upstream):
    def slow(handler, body):
        FakeUpstream.sse(handler, [{"choices": [{"delta": {"content": "partial"}}]}] +
                         [{"choices": [{"delta": {"content": "x"}}]} for _ in range(50)], delay=0.05)
    upstream.script = [slow]
    sid = new_session(client)
    say(client, sid, "cancel me")
    wait_for(lambda: upstream.requests)
    time.sleep(0.15)
    assert client.post(f"/session/{sid}/cancel", headers=A).status_code == 202
    wait_status(client, sid, "idle")
    hist = messages(client, sid)
    assert hist[-1]["message"]["status"] == "cancelled"
    assert CANCEL_NOTICE.strip() in hist[-1]["message"]["text"]["text"]


def test_cancel_outstanding_tool_calls(client, worker, upstream):
    upstream.script = [FakeUpstream.tool_reply([("read_1", "read_file", {"path": "x"})]), FakeUpstream.text_reply("next")]
    sid = new_session(client)
    say(client, sid, "read")
    wait_status(client, sid, "awaiting_tools")
    say(client, sid, "next request")
    r = client.post(f"/session/{sid}/cancel", headers=A)
    assert r.status_code == 202 and r.json["cancelled"] == "tool_calls"
    wait_status(client, sid, "idle")
    hist = messages(client, sid)
    assert any("cancelled" in m["message"]["content"][0]["text"] for m in hist if m["message"]["role"] == "tool")
    assert hist[-1]["message"]["text"]["text"] == "next"


def test_error_and_context_length_terminal(client, worker, upstream):
    upstream.script = [FakeUpstream.error(400, {"error": {"message": "context length exceeded"}})]
    sid = new_session(client)
    say(client, sid, "huge")
    wait_status(client, sid, "failed")
    hist = messages(client, sid)
    assert hist[-1]["message"]["error"]["code"] == "context_length_exceeded"
    assert len(upstream.requests) == 1
    upstream.script = [FakeUpstream.text_reply("recovered")]
    say(client, sid, "shorter")
    wait_status(client, sid, "idle")
    assert messages(client, sid)[-1]["message"]["text"]["text"] == "recovered"


def test_arena_403_not_retried(client, worker, upstream):
    upstream.script = [FakeUpstream.error(403, {"error": {"message": "restricted"}})]
    sid = new_session(client)
    say(client, sid, "hello")
    wait_status(client, sid, "failed")
    assert len(upstream.requests) == 1
    assert messages(client, sid)[-1]["message"]["error"]["code"] == "http_403"


def test_retry_503_before_stream(client, worker, upstream):
    upstream.script = [FakeUpstream.error(503, headers={"Retry-After": "0"}), FakeUpstream.text_reply("retry success")]
    sid = new_session(client)
    say(client, sid, "retry")
    wait_status(client, sid, "idle", timeout=10)
    assert len(upstream.requests) == 2
    assert messages(client, sid)[-1]["message"]["text"]["text"] == "retry success"


def test_unknown_tool_is_error_not_stuck(client, worker, upstream):
    upstream.script = [FakeUpstream.tool_reply([("x1", "nonexistent_tool", {})])]
    sid = new_session(client)
    say(client, sid, "try a tool")
    wait_status(client, sid, "failed")
    assert messages(client, sid)[-1]["message"]["error"]["code"] == "unknown_tool"
