import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from etcode_backend.api import create_app
from etcode_backend.config import Settings, parse_tokens
from etcode_backend.worker import Worker

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


class FakeUpstream:
    """Scriptable OpenAI-compatible server. ``script`` is a list of callables(handler, body)."""

    def __init__(self):
        self.requests = []
        self.script = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                step = outer.script.pop(0) if outer.script else outer.text_reply("default")
                step(self, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def sse(handler, chunks, done=True, delay=0.0):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        try:
            for chunk in chunks:
                handler.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                handler.wfile.flush()
                if delay:
                    time.sleep(delay)
            if done:
                handler.wfile.write(b"data: [DONE]\n\n")
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    @classmethod
    def text_reply(cls, text, reasoning=None, usage=None):
        def step(handler, body):
            chunks = []
            if reasoning:
                chunks.append({"choices": [{"delta": {"reasoning": reasoning}}]})
            for part in text if isinstance(text, list) else [text]:
                chunks.append({"choices": [{"delta": {"content": part}}]})
            chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            if usage is not None:
                chunks.append({"choices": [], "usage": usage})
            cls.sse(handler, chunks)
        return step

    @classmethod
    def tool_reply(cls, calls, usage=None):
        def step(handler, body):
            chunks = []
            for i, (cid, name, args) in enumerate(calls):
                s = json.dumps(args)
                chunks.append({"choices": [{"delta": {"tool_calls": [{"index": i, "id": cid, "function": {"name": name, "arguments": s[:5]}}]}}]})
                chunks.append({"choices": [{"delta": {"tool_calls": [{"index": i, "function": {"arguments": s[5:]}}]}}]})
            chunks.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
            if usage is not None:
                chunks.append({"choices": [], "usage": usage})
            cls.sse(handler, chunks)
        return step

    @staticmethod
    def error(status, body=None, headers=None):
        def step(handler, _):
            payload = json.dumps(body or {"error": {"message": f"err {status}"}}).encode()
            handler.send_response(status)
            for k, v in (headers or {}).items():
                handler.send_header(k, v)
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
        return step


@pytest.fixture
def upstream():
    fake = FakeUpstream()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def settings(tmp_path, upstream):
    return Settings(
        api_tokens=parse_tokens("alice:tok-a,bob:tok-b"),
        data_dir=Path(tmp_path),
        arena_api_key="arena-key", arena_base_url=upstream.url,
        openrouter_api_key="or-key", openrouter_base_url=upstream.url,
        worker_poll_interval=0.02, stream_poll_interval=0.02, stream_keepalive_seconds=0.5,
        stale_generation_seconds=1.0,
    )


@pytest.fixture
def client(settings):
    return create_app(settings).test_client()


@pytest.fixture
def worker(settings, client):
    w = Worker(settings)
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    yield w
    w.stop.set()
    t.join(timeout=5)


A = {"Authorization": "Bearer tok-a"}
B = {"Authorization": "Bearer tok-b"}


def new_session(client, model="arena-coding-router", headers=A, **kw):
    r = client.post("/session", json={"model": model, **kw}, headers=headers)
    assert r.status_code == 201, r.json
    return r.json["id"]


def say(client, sid, text, headers=A, **kw):
    r = client.post(f"/session/{sid}/message", json={"role": "user", "content": [{"type": "text", "text": text}], **kw}, headers=headers)
    assert r.status_code == 202, r.json
    return r.json


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        value = cond()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timeout")


def wait_status(client, sid, status, headers=A, timeout=10):
    return wait_for(lambda: client.get(f"/session/{sid}", headers=headers).json["status"] == status, timeout)


def messages(client, sid, headers=A):
    return client.get(f"/session/{sid}/messages", headers=headers).json
