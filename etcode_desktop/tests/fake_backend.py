"""A small in-process imitation of etcode_backend for tests (Flask + werkzeug, on 127.0.0.1).

It speaks the same wire format as the real backend (error bodies, SSE ids and events,
session metadata) and is scriptable:

* ``fake.add_session(...)`` makes a session. ``fake.start_generation(session)`` starts a
  generation, and ``gen.emit(kind, data)`` appends an event that is streamed live. Emitting
  ``done`` finishes the generation (it is then no longer "active", like in the real backend).
* ``fake.fail_next(...)`` makes the next matching REST requests answer with an error or stall.
* ``fake.stream_faults`` holds faults for the next stream connections: answer with an error
  status, cut the connection after N events (cleanly or abruptly), or stall.
* ``legacy=True`` imitates a backend from before ``GET /sessions`` and ``pending_tool_calls``.
* ``fake.on_submit`` is called after messages were accepted and may start a generation.

Everything the server received is in ``fake.requests``.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from flask import Flask, Response, jsonify, request
from werkzeug.datastructures import Headers
from werkzeug.exceptions import HTTPException
import socket

from werkzeug.serving import ThreadedWSGIServer

MODELS = [
    {"name": "Fake Coder", "tech_name": "fake-coder", "context_length": 100000, "supports_images": True,
     "provider": "arena", "provider_tech_name": "fake-coder-1"},
    {"name": "Fake Coder Lite", "tech_name": "fake-coder-lite", "context_length": 100000, "supports_images": True,
     "provider": "arena", "provider_tech_name": "fake-coder-lite-1"},
    {"name": "Fake Text", "tech_name": "fake-text", "context_length": 50000, "supports_images": False,
     "provider": "openrouter", "provider_tech_name": "fake/text", "openrouter_internal_provider": None},
]

BACKEND_TOOL_NAMES = {"websearch"}  # tools this fake backend runs itself

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63600000"
    "02000001e221bc330000000049454e44ae426082")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def touch_session(session: "FakeSession") -> None:
    session.updated_at = _now_iso()


def sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


@dataclass
class RecordedRequest:
    method: str
    path: str
    args: dict[str, str]
    headers: Headers  # case-insensitive, like real HTTP headers
    json: Any
    body_length: int
    raw_path: str = ""  # the request target as sent, before percent-decoding


@dataclass
class Reply:
    """What the fake model does in one generation (used when ``fake.auto_reply`` is on)."""

    text: str = "ok"
    reasoning: str = ""
    tool_calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)  # (id, name, arguments)
    chunks: int = 1  # the text is sent in this many deltas
    delay: float = 0.0  # pause after each delta (so that a test can stop it, or watch it stream)
    start_delay: float = 0.0
    error: dict[str, Any] | None = None  # {"code", "message", "retryable"}: the generation fails
    cost: str | None = "0.01"
    search_result: str = "1. Example - https://example.com"  # what a websearch call returns


CANCEL_NOTICE = "\n\n[cancelled]"


@dataclass
class RestFault:
    method: str
    pattern: re.Pattern
    status: int | None = None
    code: str = "internal_error"
    message: str = "injected failure"
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0  # seconds to wait before answering (to provoke client timeouts)
    body: str | None = None  # a raw body instead of the JSON error (a proxy's HTML page)
    remaining: int = 1


@dataclass
class StreamFault:
    """Applies to one stream connection. Faults are consumed in order, one per connection."""

    status: int | None = None  # answer with this error status instead of streaming
    code: str = "internal_error"
    cut_after: int | None = None  # stop after this many events *sent on this connection*
    abrupt: bool = False  # with cut_after: break the connection mid-response instead of ending cleanly
    hang_after: int | None = None  # after this many events, send nothing (for read-timeout tests)
    hang_seconds: float = 5.0
    event_delay: float = 0.0  # pause after each event
    keepalive_every: float | None = None  # send ": keepalive" comments while idle
    preamble: bytes = b""  # raw bytes sent before the first event


class Generation:
    def __init__(self, session: "FakeSession", linger: bool = True):
        self.id = uuid.uuid4().hex
        self.session = session
        # A real backend answers 204 once a generation is over. With `linger` the finished generation
        # stays available to the stream endpoint until `retire()`, so tests can replay it deterministically.
        self.linger = linger
        self.events: list[dict[str, Any]] = []
        self.finished = False
        self.cancel_requested = False
        self._lock = threading.Lock()

    def emit(self, kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            payload = {"generation_id": self.id, "sequence": len(self.events), **(data or {})}
            event = {"seq": len(self.events), "kind": kind, "payload": payload}
            self.events.append(event)
            if kind == "done":
                self.finished = True
                if not self.linger:
                    self.retire()
            return event

    def retire(self) -> None:
        """The generation is over for good: the stream endpoint now answers 204."""
        if self.session.active is self:
            self.session.active = None

    def snapshot(self, start: int) -> tuple[list[dict[str, Any]], bool]:
        with self._lock:
            return self.events[start:], self.finished


class FakeSession:
    def __init__(self, owner: str, model: str, websearch: bool, options: dict[str, Any]):
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.model = model
        self.websearch = websearch
        self.options = options
        self.status = "idle"
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": [{"type": "text", "text": "You are etcode."}]}]
        self.queued: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []  # {"id", "name", "arguments", "result"}
        self.active: Generation | None = None
        self.cost_exact = "0"
        self.unpriced = 0
        self.created_at = _now_iso()
        self.updated_at = self.created_at

    def title(self) -> str | None:
        for message in self.messages:
            if message["role"] == "user":
                for part in message["content"]:
                    if part["type"] == "text":
                        text = " ".join(part["text"].split())
                        if text:
                            return text[:80]
        return None


class _Server(ThreadedWSGIServer):
    """A server that can also drop the connections that are open (a real backend that stops does not keep talking)."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()

    def process_request(self, request, client_address):  # type: ignore[override]
        with self._connections_lock:
            self._connections.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request):  # type: ignore[override]
        with self._connections_lock:
            self._connections.discard(request)
        super().shutdown_request(request)

    def drop_connections(self) -> None:
        with self._connections_lock:
            open_connections = list(self._connections)
        for connection in open_connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class FakeBackend:
    def __init__(self, tokens: tuple[str, ...] = ("tok-a",), *, legacy: bool = False):
        self.tokens = set(tokens)
        self.unavailable: set[str] = set()  # tech names that the backend lists as not available (no API key)
        self.legacy = legacy
        self.sessions: dict[str, FakeSession] = {}
        self.images: dict[tuple[str, str], tuple[bytes, str]] = {}  # (token, hash) -> (bytes, mime)
        self.requests: list[RecordedRequest] = []
        self.rest_faults: list[RestFault] = []
        self.stream_faults: deque[StreamFault] = deque()
        self.stream_connections = 0
        self.auto_reply = False  # start a generation after every user message or finished tool round
        self.replies: deque[Reply] = deque()  # consumed one per generation; a plain "ok" when empty
        self.generations: list[Generation] = []
        self._state_lock = threading.RLock()
        self.on_submit: Callable[[FakeSession, list[dict[str, Any]]], None] | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self.app = self._build_app()
        self._server = _Server("127.0.0.1", 0, self.app)
        self._thread: threading.Thread | None = None
        self._port = self._server.server_port
        self._is_up = False
        self.url = f"http://127.0.0.1:{self._port}"

    # --- lifecycle --------------------------------------------------------------

    def start(self) -> "FakeBackend":
        server = self._server
        self._thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True, name="fake-backend")
        self._thread.start()
        self._is_up = True
        return self

    def stop(self) -> None:
        self._stopping.set()
        self.go_down()

    def go_down(self) -> None:
        """Stop answering: connections are dropped and new ones are refused. State is kept (``come_back`` is a restart)."""
        if not self._is_up:
            return
        self._is_up = False
        self._server.shutdown()
        self._server.server_close()
        self._server.drop_connections()
        if self._thread:
            self._thread.join(timeout=5)

    def come_back(self) -> None:
        """Answer again, on the same port, with everything that was there before."""
        if self._is_up or self._stopping.is_set():
            return
        self._server = _Server("127.0.0.1", self._port, self.app)
        self.start()

    def __enter__(self) -> "FakeBackend":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # --- scripting --------------------------------------------------------------

    def add_session(self, owner: str | None = None, model: str = "fake-coder", websearch: bool = False,
                    options: dict[str, Any] | None = None) -> FakeSession:
        session = FakeSession(owner or sorted(self.tokens)[0], model, websearch, options or {})
        self.sessions[session.id] = session
        return session

    def start_generation(self, session: FakeSession, events: list[tuple[str, dict[str, Any]]] | None = None,
                         finish: bool = False, linger: bool = True) -> Generation:
        """Start a generation. ``events`` are emitted at once. Add more later with ``gen.emit``."""
        gen = Generation(session, linger)
        self.generations.append(gen)
        session.active = gen
        session.status = "generating"
        for kind, data in events or []:
            gen.emit(kind, data)
        if finish and not gen.finished:
            gen.emit("done", {"status": "complete", "finish_reason": "stop", "next_generation_id": None})
        return gen

    def fail_next(self, method: str, pattern: str, *, status: int | None = None, code: str = "internal_error",
                  message: str = "injected failure", times: int = 1, delay: float = 0.0,
                  headers: dict[str, str] | None = None, body: str | None = None) -> None:
        self.rest_faults.append(RestFault(method.upper(), re.compile(pattern), status, code, message,
                                          headers or {}, delay, body, times))

    def requests_to(self, method: str, pattern: str) -> list[RecordedRequest]:
        regex = re.compile(pattern)
        return [r for r in self.requests if r.method == method.upper() and regex.search(r.path)]

    # --- the application --------------------------------------------------------

    def _build_app(self) -> Flask:
        app = Flask("fake_backend")
        backend = self

        @app.errorhandler(ApiError)
        def api_error(err: ApiError):
            return jsonify({"error": {"code": err.code, "message": err.message}}), err.status

        @app.errorhandler(HTTPException)
        def http_error(err: HTTPException):
            code = (err.name or "error").lower().replace(" ", "_")  # same rule as the real backend
            return jsonify({"error": {"code": code, "message": err.description}}), err.code

        @app.before_request
        def gate():
            body = request.get_data(cache=True)
            parsed = None
            if request.mimetype == "application/json" and body:
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = None
            backend.requests.append(RecordedRequest(
                request.method, request.path, request.args.to_dict(), Headers(list(request.headers)), parsed, len(body),
                request.environ.get("RAW_URI", "")))
            auth = request.headers.get("Authorization", "")
            token = auth[len("Bearer "):] if auth.startswith("Bearer ") else None
            if token is None:
                raise ApiError(401, "unauthorized", "Bearer token required")
            if token not in backend.tokens:
                raise ApiError(401, "unauthorized", "Invalid bearer token")
            request.environ["fake.token"] = token
            with backend._lock:
                for fault in backend.rest_faults:
                    if fault.method == request.method and fault.pattern.search(request.path) and fault.remaining > 0:
                        fault.remaining -= 1
                        break
                else:
                    return None
            if fault.delay:
                time.sleep(fault.delay)
            if fault.status is not None:
                if fault.body is not None:
                    return Response(fault.body, status=fault.status, mimetype="text/html", headers=fault.headers)
                response = jsonify({"error": {"code": fault.code, "message": fault.message}})
                response.status_code = fault.status
                for key, value in fault.headers.items():
                    response.headers[key] = value
                return response
            return None

        def token() -> str:
            return request.environ["fake.token"]

        def session_of(sid: str) -> FakeSession:
            session = backend.sessions.get(sid)
            if session is None or session.owner != token():
                raise ApiError(404, "session_not_found", "Session not found")
            return session

        def meta(session: FakeSession, with_title: bool = False) -> dict[str, Any]:
            data = {
                "id": session.id, "model": session.model, "websearch": session.websearch, "options": session.options,
                "status": session.status, "message_count": len(session.messages), "queued_count": len(session.queued),
                "active_generation_id": session.active.id if session.active and not session.active.finished else None,
                "cost": {"currency": "USD", "total": float(session.cost_exact), "total_exact": session.cost_exact,
                         "unpriced_generations": session.unpriced},
                "created_at": session.created_at, "updated_at": session.updated_at,
            }
            if not backend.legacy:
                data["pending_tool_calls"] = [
                    {"id": p["id"], "name": p["name"], "arguments": p["arguments"],
                     "resolved": p.get("result") is not None, "backend": p["name"] in BACKEND_TOOL_NAMES}
                    for p in session.pending]
            if with_title:
                data["title"] = session.title()
            return data

        def json_body() -> dict[str, Any]:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise ApiError(400, "invalid_json", "Request must contain a JSON object")
            return data

        def touch(session: FakeSession) -> None:
            session.updated_at = _now_iso()

        @app.get("/models")
        def models():
            return jsonify([dict(m, available=m["tech_name"] not in backend.unavailable) for m in MODELS])

        if not self.legacy:
            @app.get("/sessions")
            def list_sessions():
                def number(name: str, default: int, low: int, high: int) -> int:
                    raw = request.args.get(name)
                    if raw is None:
                        return default
                    try:
                        value = int(raw)
                    except ValueError:
                        value = low - 1
                    if not low <= value <= high:
                        raise ApiError(400, "invalid_number", f"{name} must be an integer between {low} and {high}")
                    return value

                limit = number("limit", 100, 1, 500)
                offset = number("offset", 0, 0, 1_000_000)
                mine = sorted((s for s in backend.sessions.values() if s.owner == token()),
                              key=lambda s: (s.updated_at, s.id), reverse=True)
                return jsonify({"sessions": [meta(s, with_title=True) for s in mine[offset:offset + limit]], "total": len(mine)})

        @app.post("/session")
        def create_session():
            body = json_body()
            if body.get("model") not in {m["tech_name"] for m in MODELS}:
                raise ApiError(400, "unknown_model", "Unknown model")
            options = {k: body.get(k) for k in ("temperature", "max_tokens", "reasoning_effort")}
            session = backend.add_session(token(), body["model"], bool(body.get("websearch", False)), options)
            return jsonify(meta(session)), 201

        @app.get("/session/<sid>")
        def get_session(sid: str):
            return jsonify(meta(session_of(sid)))

        @app.patch("/session/<sid>/model")
        def set_model(sid: str):
            session = session_of(sid)
            model = json_body().get("model")
            if model not in {m["tech_name"] for m in MODELS}:
                raise ApiError(400, "unknown_model", "Unknown model")
            old = next(m for m in MODELS if m["tech_name"] == session.model)
            new = next(m for m in MODELS if m["tech_name"] == model)
            if old["supports_images"] != new["supports_images"]:
                raise ApiError(409, "incompatible_model", "Cannot change between vision and non-vision models")
            if session.status not in ("idle", "failed"):
                raise ApiError(409, "session_busy", "The model can only be changed while the session is idle")
            session.model = model
            touch(session)
            return jsonify(meta(session))

        @app.get("/session/<sid>/messages")
        def all_messages(sid: str):
            session = session_of(sid)
            return jsonify([{"index": i, "message": m} for i, m in enumerate(session.messages)])

        @app.get("/session/<sid>/last/<number>")
        def last_messages(sid: str, number: str):
            session = session_of(sid)
            if not number.isdecimal() or int(number) > 1000:
                raise ApiError(400, "invalid_number", "number must be between 0 and 1000")
            count = int(number)
            start = max(len(session.messages) - count, 0) if count else len(session.messages)
            return jsonify([{"index": i, "message": session.messages[i]} for i in range(start, len(session.messages))])

        def busy(session: FakeSession) -> bool:
            return bool(session.active and not session.active.finished) or bool(session.pending)

        def submit(sid: str, body: dict[str, Any]):
            with backend._state_lock:
                return submit_locked(sid, body)

        def submit_locked(sid: str, body: dict[str, Any]):
            session = session_of(sid)
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ApiError(400, "validation_error", "messages must be a non-empty list")
            roles = {m.get("role") for m in messages if isinstance(m, dict)}
            if len(roles) != 1 or not roles <= {"user", "tool"}:
                raise ApiError(400, "validation_error", "send user messages or tool results, not both")
            accepted: list[dict[str, Any]] = []
            for message in messages:
                if not message.get("content"):
                    raise ApiError(400, "validation_error", "content must not be empty")
                if message["role"] == "tool":
                    call = next((p for p in session.pending if p["id"] == message.get("tool_call_id")), None)
                    if call is None:
                        raise ApiError(409, "unknown_tool_call", "Tool call is not awaiting a result")
                    if call["name"] in BACKEND_TOOL_NAMES:
                        raise ApiError(409, "backend_tool", f"{call['name']} is executed by the backend")
                    if call.get("result") is not None:
                        if call["result"] != message["content"]:
                            raise ApiError(409, "duplicate_tool_result", "Conflicting result for this tool call")
                        accepted.append({"tool_call_id": call["id"], "duplicate": True})
                        continue
                    call["result"] = message["content"]
                    accepted.append({"tool_call_id": call["id"], "accepted": True})
                else:
                    entry = {"queue_id": uuid.uuid4().hex}
                    if not busy(session):  # idle or failed: committed at once, like the real backend does
                        session.messages.append({"role": "user", "content": message["content"]})
                        entry.update(queued=False, index=len(session.messages) - 1)
                    else:
                        session.queued.append(message)
                        entry.update(queued=True)
                    accepted.append(entry)
            if session.pending and all(p.get("result") is not None for p in session.pending):
                for p in session.pending:
                    session.messages.append({"role": "tool", "tool_call_id": p["id"], "content": p["result"]})
                session.pending = []
                session.status = "idle"
            touch(session)
            if backend.auto_reply:
                backend._maybe_reply(session)
            if backend.on_submit is not None:
                backend.on_submit(session, messages)
            return jsonify({"accepted": accepted, "generation_id": session.active.id if session.active and not session.active.finished else None,
                            "session": meta(session)}), 202

        @app.post("/session/<sid>/message")
        def one_message(sid: str):
            body = json_body()
            overrides = {k: body.pop(k) for k in ("temperature", "max_tokens", "reasoning_effort") if k in body}
            return submit(sid, {"messages": [body], **overrides})

        @app.post("/session/<sid>/messages")
        def many_messages(sid: str):
            return submit(sid, json_body())

        @app.post("/session/<sid>/tool-calls/<call_id>/run")
        def run_tool_call(sid: str, call_id: str):
            with backend._state_lock:
                session = session_of(sid)
                call = next((p for p in session.pending if p["id"] == call_id), None)
                if call is None:
                    raise ApiError(409, "unknown_tool_call", "Tool call is not awaiting a result")
                if call["name"] not in BACKEND_TOOL_NAMES:
                    raise ApiError(409, "client_tool", f"{call['name']} is executed by the client")
                if call.get("result") is not None:
                    return jsonify({"tool_call_id": call_id, "content": call["result"], "duplicate": True,
                                    "session": meta(session)}), 200
                index = session.pending.index(call)
                if any(p.get("result") is None for p in session.pending[:index]):
                    raise ApiError(409, "tool_call_not_ready", "An earlier tool call has no result yet")
                content = [{"type": "text", "text": call.get("search_result", "1. Example - https://example.com")}]
                call["result"] = content
                if all(p.get("result") is not None for p in session.pending):
                    for pending in session.pending:
                        session.messages.append({"role": "tool", "tool_call_id": pending["id"], "content": pending["result"]})
                    session.pending = []
                    session.status = "idle"
                    touch(session)
                    if backend.auto_reply:
                        backend._maybe_reply(session)
                else:
                    touch(session)
                return jsonify({"tool_call_id": call_id, "content": content, "duplicate": False,
                                "session": meta(session)}), 200

        @app.post("/session/<sid>/cancel")
        def cancel(sid: str):
            session = session_of(sid)
            if session.active is not None:
                session.active.cancel_requested = True
                return jsonify({"cancelled": "generation", "generation_id": session.active.id}), 202
            if session.pending:
                for p in session.pending:
                    content = p.get("result") or [{"type": "text", "text": "Tool call was cancelled before a result was received."}]
                    session.messages.append({"role": "tool", "tool_call_id": p["id"], "content": content})
                session.pending = []
                session.status = "idle"
                return jsonify({"cancelled": "tool_calls", "generation_id": None}), 202
            raise ApiError(409, "no_generation", "Nothing to cancel")

        @app.get("/session/<sid>/stream")
        def stream(sid: str):
            session = session_of(sid)
            backend.stream_connections += 1
            fault = backend.stream_faults.popleft() if backend.stream_faults else StreamFault()
            if fault.status is not None:
                raise ApiError(fault.status, fault.code, "injected stream failure")
            gen = session.active
            if gen is None:
                return Response(status=204)
            start = 0
            last_id = request.headers.get("Last-Event-ID")
            if last_id:
                parts = last_id.rsplit(":", 1)
                if len(parts) != 2 or parts[0] != gen.id or not parts[1].isdecimal():
                    raise ApiError(400, "invalid_event_id", "Last-Event-ID does not belong to the active generation")
                start = int(parts[1]) + 1
            return Response(backend._stream_body(gen, start, fault), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

        @app.post("/media/images")
        def upload():
            uploaded = request.files.get("file")
            if uploaded is None:
                raise ApiError(400, "missing_file", "Multipart field 'file' is required")
            content = uploaded.stream.read()
            if not content:
                raise ApiError(413, "invalid_image", "Image is empty or exceeds the upload size limit")
            mime = sniff_image(content)
            if mime is None:
                raise ApiError(415, "unsupported_media", "Supported formats: PNG, JPEG, WebP, GIF")
            digest = hashlib.sha256(content).hexdigest()
            backend.images[(token(), digest)] = (content, mime)
            return jsonify({"hash": digest, "media_type": "image", "mime_type": mime}), 201

        @app.get("/media/images/<hash_>")
        def download(hash_: str):
            found = backend.images.get((token(), hash_))
            if found is None:
                raise ApiError(404, "image_not_found", "Image not found")
            return Response(found[0], mimetype=found[1])

        return app

    # --- scripted generations ---------------------------------------------------------------------

    def _maybe_reply(self, session: FakeSession) -> Generation | None:
        """Start a generation if the last committed message calls for one and none is running."""
        with self._state_lock:
            if session.active is not None and not session.active.finished:
                return None
            if session.pending or session.messages[-1]["role"] not in ("user", "tool"):
                return None
            spec = self.replies.popleft() if self.replies else Reply()
            gen = self.start_generation(session, linger=False)  # like the real backend: 204 once it is over
            threading.Thread(target=self._run_reply, args=(session, gen, spec), daemon=True, name="fake-generation").start()
            return gen

    @staticmethod
    def _assistant(text: str, reasoning: str, calls: list[dict[str, Any]], status: str, error: dict[str, Any] | None) -> dict[str, Any]:
        return {"role": "assistant", "text": {"type": "text", "text": text}, "reasoning": {"type": "text", "text": reasoning},
                "tool_calls": calls, "status": status, "error": error}

    def _run_reply(self, session: FakeSession, gen: Generation, spec: Reply) -> None:
        if spec.start_delay:
            time.sleep(spec.start_delay)
        sent = ""
        if spec.reasoning:
            gen.emit("reasoning_delta", {"text": spec.reasoning})
        size = max(1, -(-len(spec.text) // max(spec.chunks, 1)))
        for start in range(0, len(spec.text), size):
            if gen.cancel_requested or self._stopping.is_set():
                break
            part = spec.text[start:start + size]
            gen.emit("text_delta", {"text": part})
            sent += part
            if spec.delay:
                time.sleep(spec.delay)
        with self._state_lock:
            self._finish_reply(session, gen, spec, sent)

    def _finish_reply(self, session: FakeSession, gen: Generation, spec: Reply, sent: str) -> None:
        def commit(message: dict[str, Any], finish_reason: str) -> int:
            session.messages.append(message)
            index = len(session.messages) - 1
            gen.emit("message_complete", {"index": index, "finish_reason": finish_reason, "message": message})
            return index

        def finish(status: str, finish_reason: str) -> None:
            next_id = None
            if session.queued:
                for queued in session.queued:
                    session.messages.append({"role": "user", "content": queued["content"]})
                session.queued = []
                session.status = "idle"
                gen.retire()  # this generation is over: the next one may start before its `done` is published
                nxt = self._maybe_reply(session)
                next_id = nxt.id if nxt else None
            else:
                session.status = "idle"
            if status == "error" and next_id is None:
                session.status = "failed"
            touch_session(session)
            gen.emit("done", {"status": status, "finish_reason": finish_reason, "next_generation_id": next_id})

        if gen.cancel_requested:
            commit(self._assistant(sent + CANCEL_NOTICE, spec.reasoning, [], "cancelled", None), "cancelled")
            gen.emit("cancelled", {"index": len(session.messages) - 1})
            finish("cancelled", "cancelled")
            return
        if spec.error is not None:
            commit(self._assistant(sent, spec.reasoning, [], "error", spec.error), "error")
            gen.emit("error", dict(spec.error))
            finish("error", "error")
            return
        if spec.cost is not None:
            session.cost_exact = format(Decimal(session.cost_exact) + Decimal(spec.cost), "f")
        gen.emit("usage", {"cost": spec.cost, "currency": "USD", "session_cost_total": session.cost_exact,
                           "source": "provider" if spec.cost is not None else "unknown", "usage": {}})
        calls = [{"id": cid, "name": name, "arguments": args} for cid, name, args in spec.tool_calls]
        message = self._assistant(sent or spec.text, spec.reasoning, calls, "complete", None)
        if not calls:
            commit(message, "stop")
            finish("complete", "stop")
            return
        session.pending = [{"id": c["id"], "name": c["name"], "arguments": c["arguments"],
                            "search_result": spec.search_result} for c in calls]
        for call in calls:
            gen.emit("tool_call", {"tool_call_id": call["id"], "name": call["name"], "arguments": call["arguments"]})
        commit(message, "tool_calls")
        if self.legacy:  # the old backend runs web searches by itself
            for pending in session.pending:
                if pending["name"] == "websearch":
                    pending["result"] = [{"type": "text", "text": spec.search_result}]
                    gen.emit("tool_result", {"tool_call_id": pending["id"], "name": "websearch", "content": pending["result"]})
            if all(p.get("result") is not None for p in session.pending):
                for pending in session.pending:
                    session.messages.append({"role": "tool", "tool_call_id": pending["id"], "content": pending["result"]})
                session.pending = []
                session.status = "idle"
                gen.retire()
                nxt = self._maybe_reply(session)
                gen.emit("done", {"status": "tool_calls", "finish_reason": "tool_calls", "next_generation_id": nxt.id if nxt else None})
                return
        session.status = "awaiting_tools"
        touch_session(session)
        gen.emit("done", {"status": "tool_calls", "finish_reason": "tool_calls", "next_generation_id": None})

    def _stream_body(self, gen: Generation, start: int, fault: StreamFault):
        sent = 0
        index = start
        last_activity = time.monotonic()
        if fault.preamble:
            yield fault.preamble
        while not self._stopping.is_set():
            events, finished = gen.snapshot(index)
            for event in events:
                if fault.cut_after is not None and sent >= fault.cut_after:
                    if fault.abrupt:
                        raise ConnectionResetError("injected: connection broken")
                    return
                if fault.hang_after is not None and sent >= fault.hang_after:
                    self._stopping.wait(fault.hang_seconds)
                    return
                data = json.dumps(event["payload"], ensure_ascii=False)
                yield f"id: {gen.id}:{event['seq']}\nevent: {event['kind']}\ndata: {data}\n\n".encode()
                sent += 1
                index += 1
                last_activity = time.monotonic()
                if fault.event_delay:
                    time.sleep(fault.event_delay)
            if not events:
                if finished:
                    return
                if fault.cut_after is not None and sent >= fault.cut_after:
                    if fault.abrupt:
                        raise ConnectionResetError("injected: connection broken")
                    return
                if fault.hang_after is not None and sent >= fault.hang_after:
                    self._stopping.wait(fault.hang_seconds)
                    return
                if fault.keepalive_every and time.monotonic() - last_activity >= fault.keepalive_every:
                    yield b": keepalive\n\n"
                    last_activity = time.monotonic()
                time.sleep(0.005)
