"""The real etcode_backend (Flask app + worker) with a fake LLM upstream, for tests.

Skipped (at import) if the backend package or its dependencies are not available. The repository
layout is assumed: ``../../etcode_backend`` next to this project.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "etcode_backend"
if BACKEND_DIR.is_dir():
    sys.path.insert(0, str(BACKEND_DIR))

pytest.importorskip("etcode_backend", reason="etcode_backend is not next to etcode_desktop")
pytest.importorskip("flask")
pytest.importorskip("pydantic")

from werkzeug.serving import make_server  # noqa: E402

from etcode_backend.api import create_app  # noqa: E402
from etcode_backend.config import Settings, parse_tokens  # noqa: E402
from etcode_backend.worker import Worker  # noqa: E402


class Upstream:
    """A scriptable OpenAI-compatible chat completions server (the LLM provider)."""

    def __init__(self):
        self.script = []
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                step = outer.script.pop(0) if outer.script else outer.text("default reply")
                step(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True).start()

    @staticmethod
    def _stream(handler, chunks, delay=0.0):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        try:
            for chunk in chunks:
                handler.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                handler.wfile.flush()
                if delay:
                    time.sleep(delay)
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    @classmethod
    def text(cls, parts, delay=0.0):
        parts = parts if isinstance(parts, list) else [parts]

        def step(handler):
            chunks = [{"choices": [{"delta": {"content": p}}]} for p in parts]
            chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            chunks.append({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7, "cost": 0.0042}})
            cls._stream(handler, chunks, delay)
        return step

    @classmethod
    def tools(cls, calls):
        def step(handler):
            chunks = []
            for i, (call_id, name, arguments) in enumerate(calls):
                text = json.dumps(arguments)
                chunks.append({"choices": [{"delta": {"tool_calls": [{"index": i, "id": call_id, "function": {"name": name, "arguments": text[:4]}}]}}]})
                chunks.append({"choices": [{"delta": {"tool_calls": [{"index": i, "function": {"arguments": text[4:]}}]}}]})
            chunks.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
            cls._stream(handler, chunks)
        return step


class RealBackend:
    def __init__(self, tmp_path):
        self.upstream = Upstream()
        self.settings = Settings(
            api_tokens=parse_tokens("alice:tok-a,bob:tok-b"), data_dir=Path(tmp_path),
            arena_api_key="arena-key", arena_base_url=self.upstream.url,
            openrouter_api_key="or-key", openrouter_base_url=self.upstream.url,
            worker_poll_interval=0.02, stream_poll_interval=0.02, stream_keepalive_seconds=0.5,
            stale_generation_seconds=5.0,
        )
        self.app = create_app(self.settings)
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self._serve()
        self.worker = Worker(self.settings)
        self.worker_thread = threading.Thread(target=self.worker.run, daemon=True)
        self.worker_thread.start()

    def _serve(self):
        server = self.server
        threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True).start()

    def go_down(self):
        """The HTTP server stops (the worker and the database go on, as when only the API process is restarted)."""
        self.server.shutdown()
        self.server.server_close()

    def come_back(self):
        self.server = make_server("127.0.0.1", self.port, self.app, threaded=True)
        self._serve()

    def stop(self):
        self.worker.stop.set()
        self.worker_thread.join(timeout=5)
        try:
            self.server.shutdown()
            self.server.server_close()
        except OSError:
            pass
        self.upstream.server.shutdown()
        self.upstream.server.server_close()
