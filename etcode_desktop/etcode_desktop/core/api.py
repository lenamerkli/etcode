"""HTTP client for etcode_backend. One method per endpoint, typed results, typed errors.

The client is blocking and thread-safe: every thread gets its own ``requests.Session``,
because a session object is not safe to share. The server-sent event stream is parsed
in :mod:`etcode_desktop.core.sse`. This module only opens it (:meth:`BackendClient.open_stream`).

Retries
-------
Requests that are safe to repeat are retried with a short backoff when the backend can't
be reached or answers 502/503/504: every GET, image uploads (they are stored by content
hash), and tool results (the backend accepts an identical repeat as a duplicate). A user
message is **never** retried automatically, because a repeat after an unknown outcome
could send it twice.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests
import urllib3

from .. import __version__
from ..logging_setup import register_secret
from .errors import (
    AuthError, BackendError, Conflict, NotFound, ProtocolError, ServerError, Unreachable, Unsupported,
)
from .models import (
    BackendToolResult, DownloadedImage, HistoryEntry, ModelInfo, SessionList, SessionMeta, SubmitResult,
    UploadedImage,
)

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({502, 503, 504})
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ERROR_SNIPPET_CHARS = 200


@dataclass(frozen=True)
class Timeouts:
    connect: float = 5.0
    read: float = 30.0
    upload_read: float = 120.0  # a large image on a slow link


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3  # in total, including the first try
    base_delay: float = 0.5
    max_delay: float = 4.0


def error_from_response(response: requests.Response) -> BackendError:
    """Turn an error response into the matching exception.

    The backend sends ``{"error": {"code", "message"}}``. Anything else (a reverse proxy's
    HTML page, say) gets the code ``http_<status>`` and the start of the body as message.
    """
    status = response.status_code
    code, message = f"http_{status}", response.reason or "request failed"
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])
    except (ValueError, KeyError, TypeError):
        try:
            snippet = response.text.strip()[:_ERROR_SNIPPET_CHARS]
        except Exception:  # an undecodable body must not hide the original error
            snippet = ""
        if snippet:
            message = snippet
    cls: type[BackendError]
    if status == 401:
        cls = AuthError
    elif status == 404:
        cls = NotFound
    elif status == 409:
        cls = Conflict
    elif status >= 500:
        cls = ServerError
    else:
        cls = BackendError
    return cls(status, code, message)


def describe_network_error(exc: BaseException, base_url: str) -> str:
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return f"timed out connecting to {base_url}"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return f"{base_url} did not answer in time"
    if isinstance(exc, requests.exceptions.SSLError):
        return f"TLS error talking to {base_url}: {exc}"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return f"cannot connect to {base_url}"
    return f"network error talking to {base_url}: {exc}"


def _find_socket(response: requests.Response) -> socket.socket | None:
    """The socket under a streaming response, if we can reach it.

    Closing a socket from another thread does not wake up a thread blocked in ``recv`` on
    Linux, but ``shutdown`` does. Stop buttons need that, so we look for the socket in
    the places where urllib3 keeps it. Failing to find it is harmless: ``close`` then works
    as well as it can (the read ends when data arrives or the read timeout passes).
    """
    raw = getattr(response, "raw", None)
    candidates = (
        getattr(getattr(raw, "_connection", None), "sock", None),  # urllib3 2.x
        getattr(getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None), "_sock", None),  # http.client
    )
    for candidate in candidates:
        if isinstance(candidate, socket.socket):
            return candidate
    return None


class StreamConnection:
    """An open ``GET /session/<id>/stream`` response."""

    def __init__(self, response: requests.Response, base_url: str):
        self._response = response
        self._base_url = base_url
        self._socket = _find_socket(response)
        self._closed = threading.Event()  # set by close(), possibly from another thread
        self.no_content = response.status_code == 204  # no generation is active

    def chunks(self) -> Iterator[bytes]:
        """Raw bytes as they arrive. A broken connection raises :class:`Unreachable`."""
        if self.no_content or self._closed.is_set():
            return
        try:
            yield from self._response.iter_content(chunk_size=None)
        except (requests.exceptions.RequestException, urllib3.exceptions.HTTPError, OSError, ValueError) as exc:
            raise Unreachable(describe_network_error(exc, self._base_url)) from exc
        except Exception as exc:
            # urllib3 can fail in odd ways (an AttributeError, say) when the response is closed
            # underneath a reader. That is our own close(), not a bug, so it counts as a lost connection.
            if self._closed.is_set():
                raise Unreachable("the connection was closed") from exc
            raise

    def close(self) -> None:
        """Close the connection. Safe to call from another thread: it ends a blocked read."""
        self._closed.set()
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:  # already closed or not connected
                pass
        try:
            self._response.close()
        except Exception:  # closing a connection that is already broken is fine
            pass


class BackendClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeouts: Timeouts = Timeouts(),
        retry: RetryPolicy = RetryPolicy(),
        stream_read_timeout: float = 45.0,  # the backend sends a keepalive every 15 s
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeouts = timeouts
        self._retry = retry
        self._stream_read_timeout = stream_read_timeout
        self._sleep = sleep
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()
        register_secret(token)

    @classmethod
    def from_settings(cls, settings: Any, **kwargs: Any) -> BackendClient:
        return cls(settings.backend_url, settings.token, **kwargs)

    def __repr__(self) -> str:
        return f"BackendClient(base_url={self.base_url!r}, token={'<set>' if self._token else '<none>'})"

    def close(self) -> None:
        with self._sessions_lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()
        self._local = threading.local()

    # --- plumbing ---------------------------------------------------------------

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["Authorization"] = f"Bearer {self._token}"
            session.headers["User-Agent"] = f"etcode-desktop/{__version__}"
            session.headers["Accept"] = "application/json"
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        files: Any = None,
        headers: Mapping[str, str] | None = None,
        stream: bool = False,
        timeout: tuple[float, float] | None = None,
    ) -> requests.Response:
        try:
            response = self._session().request(
                method, self._url(path), params=params, json=json, files=files, headers=headers,
                stream=stream, timeout=timeout or (self._timeouts.connect, self._timeouts.read),
            )
        except requests.exceptions.RequestException as exc:
            raise Unreachable(describe_network_error(exc, self.base_url)) from exc
        if response.status_code >= 400:
            try:
                raise error_from_response(response)
            finally:
                response.close()
        return response

    def _call(self, method: str, path: str, *, retry: bool = False, **kwargs: Any) -> requests.Response:
        attempt = 0
        while True:
            try:
                return self._send(method, path, **kwargs)
            except (Unreachable, ServerError) as exc:
                if isinstance(exc, ServerError) and exc.status not in RETRYABLE_STATUS:
                    raise
                attempt += 1
                if not retry or attempt >= self._retry.attempts:
                    raise
                delay = min(self._retry.base_delay * 2 ** (attempt - 1), self._retry.max_delay)
                log.info("%s %s failed (%s), retrying in %.1fs (%d/%d)", method, path, exc, delay, attempt, self._retry.attempts - 1)
                self._sleep(delay)

    @staticmethod
    def _json(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ProtocolError(f"the backend sent invalid JSON (HTTP {response.status_code})") from exc

    @staticmethod
    def _sid(session_id: str) -> str:
        return quote(session_id, safe="")

    # --- catalog and sessions ---------------------------------------------------------

    def models(self) -> list[ModelInfo]:
        data = self._json(self._call("GET", "/models", retry=True))
        if not isinstance(data, list):
            raise ProtocolError("/models did not return a list")
        return [ModelInfo.from_dict(m) for m in data]

    def list_sessions(self, limit: int = 100, offset: int = 0) -> SessionList:
        """The caller's sessions, newest activity first.

        Raises :class:`Unsupported` on a backend without ``GET /sessions``. The caller then
        works from the local session store only.
        """
        try:
            response = self._call("GET", "/sessions", retry=True, params={"limit": limit, "offset": offset})
        except BackendError as exc:
            if exc.status in (404, 405):
                raise Unsupported(exc.status, exc.code, "this backend cannot list sessions") from exc
            raise
        return SessionList.from_json(self._json(response))

    def create_session(
        self,
        model: str,
        *,
        websearch: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> SessionMeta:
        body: dict[str, Any] = {"model": model, "websearch": websearch}
        for key, value in (("temperature", temperature), ("max_tokens", max_tokens), ("reasoning_effort", reasoning_effort)):
            if value is not None:
                body[key] = value
        # Not retried: a repeat after an unknown outcome would leave an orphan session behind.
        return SessionMeta.from_dict(self._json(self._call("POST", "/session", json=body)))

    def get_session(self, session_id: str) -> SessionMeta:
        return SessionMeta.from_dict(self._json(self._call("GET", f"/session/{self._sid(session_id)}", retry=True)))

    def set_model(self, session_id: str, model: str) -> SessionMeta:
        response = self._call("PATCH", f"/session/{self._sid(session_id)}/model", json={"model": model})
        return SessionMeta.from_dict(self._json(response))

    # --- history ----------------------------------------------------------------------

    def messages(self, session_id: str) -> list[HistoryEntry]:
        return self._entries(self._call("GET", f"/session/{self._sid(session_id)}/messages", retry=True))

    def last(self, session_id: str, count: int) -> list[HistoryEntry]:
        return self._entries(self._call("GET", f"/session/{self._sid(session_id)}/last/{int(count)}", retry=True))

    def _entries(self, response: requests.Response) -> list[HistoryEntry]:
        data = self._json(response)
        if not isinstance(data, list):
            raise ProtocolError("the message history is not a list")
        return [HistoryEntry.from_dict(e) for e in data]

    # --- sending ----------------------------------------------------------------------

    def submit(
        self,
        session_id: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> SubmitResult:
        """Send user messages or tool results (not both in one call; the backend refuses that).

        Build the messages with :func:`~etcode_desktop.core.models.user_message` and
        :func:`~etcode_desktop.core.models.tool_message`. Tool results are retried on
        failure, user messages are not (see the module docstring).
        """
        body: dict[str, Any] = {"messages": [dict(m) for m in messages]}
        for key, value in (("temperature", temperature), ("max_tokens", max_tokens), ("reasoning_effort", reasoning_effort)):
            if value is not None:
                body[key] = value
        only_tool_results = bool(messages) and all(m.get("role") == "tool" for m in messages)
        response = self._call("POST", f"/session/{self._sid(session_id)}/messages", json=body, retry=only_tool_results)
        return SubmitResult.from_dict(self._json(response))

    def run_tool_call(self, session_id: str, call_id: str) -> BackendToolResult:
        """Ask the backend to run one of its own tool calls (websearch, ...) and return the result.

        Retried on network trouble: the backend returns the stored result as a duplicate if the
        first request was executed but its answer was lost.
        """
        path = f"/session/{self._sid(session_id)}/tool-calls/{quote(call_id, safe='')}/run"
        return BackendToolResult.from_dict(self._json(self._call("POST", path, retry=True)))

    def cancel(self, session_id: str) -> dict[str, Any]:
        """Cancel the running generation, or abandon outstanding tool calls.

        Raises :class:`Conflict` (code ``no_generation``) if there is nothing to cancel.
        """
        data = self._json(self._call("POST", f"/session/{self._sid(session_id)}/cancel"))
        return data if isinstance(data, dict) else {}

    def open_stream(self, session_id: str, last_event_id: str | None = None) -> StreamConnection:
        """Open the event stream of the active generation (``no_content`` is true if there is none).

        There is no retry here. :class:`~etcode_desktop.core.sse.EventFollower` handles reconnecting.
        """
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        response = self._send(
            "GET", f"/session/{self._sid(session_id)}/stream", headers=headers, stream=True,
            timeout=(self._timeouts.connect, self._stream_read_timeout),
        )
        return StreamConnection(response, self.base_url)

    # --- media ------------------------------------------------------------------------

    def upload_image(self, data: bytes, filename: str = "image") -> UploadedImage:
        """Upload image bytes (PNG, JPEG, WebP or GIF). The backend checks the real content type."""
        response = self._call(
            "POST", "/media/images", retry=True,
            files={"file": (filename, data, "application/octet-stream")},
            timeout=(self._timeouts.connect, self._timeouts.upload_read),
        )
        return UploadedImage.from_dict(self._json(response))

    def download_image(self, image_hash: str) -> DownloadedImage:
        if not _HASH.fullmatch(image_hash):
            raise ValueError("an image hash is 64 lowercase hex characters")
        response = self._call("GET", f"/media/images/{image_hash}", retry=True,
                              timeout=(self._timeouts.connect, self._timeouts.upload_read))
        try:
            content = response.content
        except requests.exceptions.RequestException as exc:
            raise Unreachable(describe_network_error(exc, self.base_url)) from exc
        return DownloadedImage(content, response.headers.get("Content-Type", "application/octet-stream"))
