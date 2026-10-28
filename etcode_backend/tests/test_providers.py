import json
import threading

import pytest

from conftest import A, FakeUpstream, messages, new_session, say, wait_status
from etcode_backend.catalog import get_model
from etcode_backend.config import parse_tokens
from etcode_backend.db import connect, transaction, now
from etcode_backend.errors import ProviderError
from etcode_backend.providers import CancelToken, get_provider
from etcode_backend.search import SearchQuery


def test_parse_tokens():
    tokens = parse_tokens("alice:abc, def\nbob:x:y")
    assert tokens["abc"] == "name:alice"
    assert tokens["def"].startswith("sha256:")
    assert tokens["x:y"] == "name:bob"
    with pytest.raises(ValueError):
        parse_tokens("alice:")


def test_search_query_validation():
    assert SearchQuery(query="x", freshness="2024-01-01to2024-02-01").count == 10
    for bad in ({"query": ""}, {"query": "a " * 51}, {"query": "x", "count": 21},
                {"query": "x", "offset": 10}, {"query": "x", "freshness": "yesterday"}):
        with pytest.raises(Exception):
            SearchQuery(**bad)


def test_message_conversion(settings):
    provider = get_provider("openrouter", settings)
    hist = [
        {"role": "system", "content": [{"type": "text", "text": "sys"}]},
        {"role": "user", "content": [{"type": "text", "text": "u"}, {"type": "image", "hash": "h1"}]},
        {"role": "assistant", "text": {"type": "text", "text": ""}, "reasoning": {"type": "text", "text": "r"},
         "tool_calls": [{"id": "c1", "name": "view_image", "arguments": {"path": "p"}}], "status": "complete", "error": None},
        {"role": "tool", "tool_call_id": "c1", "content": [{"type": "image", "hash": "h2"}]},
        {"role": "assistant", "text": {"type": "text", "text": ""}, "reasoning": {"type": "text", "text": ""},
         "tool_calls": [], "status": "error", "error": {"code": "x"}},
    ]
    out = provider.convert_messages(hist, lambda h: ("image/png", b"img"))
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "user"]
    assert out[1]["content"][1]["image_url"]["url"] == "data:image/png;base64,aW1n"
    assert out[2]["content"] is None and json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"path": "p"}
    assert isinstance(out[3]["content"], str) and "image" in out[3]["content"]
    assert out[4]["content"][1]["type"] == "image_url"


def test_interrupted_stream_after_output_not_retried(settings, upstream):
    def partial(handler, body):
        FakeUpstream.sse(handler, [{"choices": [{"delta": {"content": "half"}}]}], done=False)
    upstream.script = [partial, FakeUpstream.text_reply("should not be used")]
    provider = get_provider("arena", settings)
    events = []
    with pytest.raises(ProviderError) as err:
        for event in provider.stream(get_model("arena-coding-router"), [], [], {}, lambda h: None, CancelToken()):
            events.append(event)
    assert err.value.code == "stream_interrupted"
    assert [e.data for e in events] == ["half"]
    assert len(upstream.requests) == 1


def test_mid_stream_error_chunk(settings, upstream):
    def bad(handler, body):
        FakeUpstream.sse(handler, [{"error": {"message": "overloaded", "code": 502}}], done=False)
    upstream.script = [bad, FakeUpstream.text_reply("ok")]
    provider = get_provider("openrouter", settings)
    out = list(provider.stream(get_model("deepseek-v4.1-flash-1017"), [], [], {}, lambda h: None, CancelToken()))
    assert [e.data for e in out if e.kind == "text"] == ["ok"]
    assert len(upstream.requests) == 2


def test_arena_request_shape(settings, upstream):
    upstream.script = [FakeUpstream.text_reply("x")]
    provider = get_provider("arena", settings)
    list(provider.stream(get_model("arena-coding-router"), [], [{"type": "function", "function": {"name": "bash"}}],
                         {"reasoning_effort": "low"}, lambda h: None, CancelToken()))
    req = upstream.requests[0]
    assert req["path"] == "/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer arena-key"
    assert req["body"]["model"] == "coding-router-preview"
    assert req["body"]["reasoning_effort"] == "low"


def test_restart_recovery_marks_interrupted(client, settings, upstream):
    sid = new_session(client)
    say(client, sid, "hi")  # no worker running: generation stays queued
    with connect(settings) as con:
        con.execute("UPDATE generations SET status='running', updated_at=? WHERE session_id=?", (now() - 100, sid))
    from etcode_backend.worker import Worker
    worker = Worker(settings)
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    try:
        wait_status(client, sid, "failed")
        last = messages(client, sid)[-1]["message"]
        assert last["status"] == "error" and last["error"]["code"] == "interrupted"
        assert not upstream.requests
    finally:
        worker.stop.set()
        t.join(5)


def test_concurrent_sessions(client, worker, upstream):
    def slow(handler, body):
        FakeUpstream.sse(handler, [{"choices": [{"delta": {"content": "a"}}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}]}], delay=0.3)
    upstream.script = [slow, slow, slow]
    sids = [new_session(client) for _ in range(3)]
    for sid in sids:
        say(client, sid, "go")
    for sid in sids:
        wait_status(client, sid, "idle", timeout=3)


def test_small_deltas_are_not_buffered(settings, upstream):
    """Each tiny delta must reach the worker before the upstream sends the next one."""
    import time as _t
    gate = threading.Event()

    def step(handler, body):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        handler.wfile.write(b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n')
        handler.wfile.flush()
        gate.wait(5)  # hold the rest until the first delta has been consumed
        handler.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        handler.wfile.flush()
    upstream.script = [step]
    provider = get_provider("arena", settings)
    stream = provider.stream(get_model("arena-coding-router"), [], [], {}, lambda h: None, CancelToken())
    started = _t.monotonic()
    first = next(stream)
    assert first.data == "a" and _t.monotonic() - started < 2
    gate.set()
    assert [e.kind for e in stream] == ["finish"]
