"""Server-sent events: a parser, and a follower that survives dropped connections.

:func:`split_lines` and :func:`parse_events` implement the wire format
(https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream).
:class:`EventFollower` reads one generation's events from the backend and reconnects with
``Last-Event-ID`` when the connection drops, so the consumer sees each event once, in order.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .api import BackendClient, StreamConnection
from .errors import BackendError, ServerError, Unreachable
from .models import DONE, StreamEvent

log = logging.getLogger(__name__)

_EOL = re.compile(rb"\r\n|\r|\n")

OUTCOME_DONE = "done"  # the generation's `done` event arrived
OUTCOME_NO_GENERATION = "no_active_generation"  # the backend answered 204: nothing to follow
OUTCOME_STOPPED = "stopped"  # close() was called


# --- wire format -------------------------------------------------------------------------

def split_lines(chunks: Iterable[bytes]) -> Iterator[str]:
    """Split a byte stream into lines. A line ends with ``\\n``, ``\\r\\n`` or ``\\r``.

    Only those three count, as the SSE format says. ``str.splitlines`` would also split on
    U+2028, U+0085, form feeds and so on, which can appear inside a JSON payload.
    """
    buffer = b""
    skip_lf = False  # the previous chunk ended with "\r": swallow a "\n" that starts this one
    for chunk in chunks:
        if not chunk:
            continue
        if skip_lf:
            if chunk.startswith(b"\n"):
                chunk = chunk[1:]
            skip_lf = False
        buffer += chunk
        start = 0
        while True:
            match = _EOL.search(buffer, start)
            if match is None:
                break
            yield buffer[start:match.start()].decode("utf-8", errors="replace")
            start = match.end()
            if match.group() == b"\r" and start == len(buffer):
                skip_lf = True  # it may be the first half of "\r\n"
                break
        buffer = buffer[start:]
    if buffer:
        yield buffer.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class RawEvent:
    event: str
    data: str
    id: str | None


def parse_events(lines: Iterable[str]) -> Iterator[RawEvent]:
    """Group lines into events. Comments (keepalives), ``retry`` and unknown fields are ignored."""
    event_type = ""
    data: list[str] = []
    event_id: str | None = None
    for line in lines:
        if line == "":
            if data:  # an event without data is not dispatched
                yield RawEvent(event_type or "message", "\n".join(data), event_id)
            event_type, data, event_id = "", [], None
            continue
        if line.startswith(":"):
            continue
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "event":
            event_type = value
        elif name == "data":
            data.append(value)
        elif name == "id" and "\0" not in value:
            event_id = value
        # "retry" and unknown fields: ignored
    # an event that is not terminated by an empty line was cut off, and is dropped


def parse_event_id(event_id: str | None) -> tuple[str, int] | None:
    """``"<generation_id>:<sequence>"`` into its parts, or None."""
    if not event_id:
        return None
    generation, sep, sequence = event_id.rpartition(":")
    if not sep or not generation or not sequence.isdecimal():
        return None
    return generation, int(sequence)


def decode_event(raw: RawEvent) -> StreamEvent | None:
    """Decode the JSON payload. Returns None (and logs) for an event we can't use."""
    try:
        payload: Any = json.loads(raw.data)
    except json.JSONDecodeError:
        log.warning("dropping a %r event with invalid JSON", raw.event)
        return None
    if not isinstance(payload, dict):
        log.warning("dropping a %r event whose payload is not an object", raw.event)
        return None
    generation = payload.get("generation_id")
    sequence = payload.get("sequence")
    from_id = parse_event_id(raw.id)
    if from_id is not None:
        generation = generation if isinstance(generation, str) else from_id[0]
        sequence = sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else from_id[1]
    return StreamEvent(
        kind=raw.event,
        data=payload,
        event_id=raw.id,
        generation_id=generation if isinstance(generation, str) else None,
        sequence=sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else None,
    )


# --- following a generation ----------------------------------------------------------------

@dataclass(frozen=True)
class Backoff:
    initial: float = 1.0
    factor: float = 2.0
    maximum: float = 15.0
    give_up_after: float = 120.0  # seconds of failed attempts in a row, then Unreachable is raised


class EventFollower:
    """Iterate over the events of a session's active generation.

    ::

        follower = EventFollower(client, session_id)
        for event in follower:
            ...                       # `done` is the last event
        follower.outcome              # "done", "no_active_generation" or "stopped"

    * A lost connection (network error, read timeout, server closing without ``done``) is
      followed by a reconnect with ``Last-Event-ID``. The events then continue where they
      stopped. If the backend can't continue from there (400 ``invalid_event_id``), the
      generation is replayed from its start, and events already delivered are skipped.
      The consumer therefore never sees an event twice.
    * Retrying stops with :class:`Unreachable` after ``backoff.give_up_after`` seconds
      without a working connection. 401 and 404 are raised at once.
    * Events carry their ``generation_id``. If the backend moves on to another generation
      while we were reconnecting, the events of the new one follow, so a consumer should look
      at ``generation_id`` and not assume that it saw the previous generation's ``done``.
    * An outcome of ``no_active_generation`` means the backend has no active generation. That
      is normal right after a ``done``, or when a generation finished while we were
      reconnecting. Ask ``GET /session`` what to do next.
    * ``on_interrupted(error, delay)`` is called (on the iterating thread) before each wait for a
      reconnect, and ``on_resumed()`` when a connection works again after that. A GUI can show and hide
      a "reconnecting" notice with them.
    * :meth:`retry_now` (from another thread) ends the wait for the next attempt, for example when the
      user presses "retry" or the network is known to be back.
    * :meth:`close` may be called from another thread. It interrupts a read in progress.
    """

    def __init__(
        self,
        client: BackendClient,
        session_id: str,
        *,
        since: str | None = None,
        backoff: Backoff = Backoff(),
        stop: threading.Event | None = None,
        on_interrupted: Callable[[Exception, float], None] | None = None,
        on_resumed: Callable[[], None] | None = None,
    ):
        self._client = client
        self._session_id = session_id
        self._backoff = backoff
        self._stop = stop or threading.Event()
        self._lock = threading.Lock()
        self._connection: StreamConnection | None = None
        self._last_event_id = since
        self._on_interrupted = on_interrupted
        self._on_resumed = on_resumed
        self._interrupted = False  # a connection was lost and has not come back yet
        self._wake = threading.Event()  # set by retry_now() and close(): ends a wait for the next attempt
        self._delivered: dict[str, int] = {}  # generation id -> highest sequence handed out
        self.outcome: str | None = None
        self.last_event: StreamEvent | None = None

    @property
    def last_event_id(self) -> str | None:
        return self._last_event_id

    def retry_now(self) -> None:
        """Do not wait any longer for the next attempt to reconnect (a no-op while connected). Thread safe."""
        self._wake.set()

    def _sleep(self, delay: float) -> bool:
        """Wait for ``delay`` seconds, or until :meth:`retry_now` or stop. Returns True if stopped."""
        end = time.monotonic() + delay
        while True:
            if self._stop.is_set():
                return True
            if self._wake.is_set():
                self._wake.clear()
                return False
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            self._wake.wait(min(remaining, 0.25))  # in slices, so that an external stop event is noticed

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            connection = self._connection
        if connection is not None:
            connection.close()

    # --- iteration --------------------------------------------------------------

    def __iter__(self) -> Iterator[StreamEvent]:
        failing_since: float | None = None  # when the current run of failed attempts began
        delay = self._backoff.initial
        replayed = False  # already fell back to "replay from the start" once for this problem
        while not self._stop.is_set():
            progress = _Progress()
            error: Exception
            try:
                connection = self._client.open_stream(self._session_id, self._last_event_id)
            except BackendError as exc:
                if exc.code == "invalid_event_id" and self._last_event_id is not None and not replayed:
                    log.info("cannot resume after %s, replaying the generation from its start", self._last_event_id)
                    self._last_event_id = None
                    replayed = True
                    continue
                if not isinstance(exc, ServerError):
                    raise  # 401, 404 and other client errors will not get better by waiting
                error = exc
            except Unreachable as exc:
                error = exc
            else:
                replayed = False
                if self._interrupted:
                    self._interrupted = False
                    if self._on_resumed is not None:
                        self._on_resumed()
                with self._lock:
                    self._connection = connection
                try:
                    if self._stop.is_set():
                        break
                    if connection.no_content:
                        self.outcome = OUTCOME_NO_GENERATION
                        return
                    try:
                        for raw in parse_events(split_lines(progress.count(connection.chunks()))):
                            event = decode_event(raw)
                            if event is None or self._seen(event):
                                continue
                            self._remember(event)
                            yield event
                            if event.kind == DONE:
                                self.outcome = OUTCOME_DONE
                                return
                        error = Unreachable("the event stream ended before the generation finished")
                    except Unreachable as exc:
                        error = exc
                finally:
                    connection.close()
                    with self._lock:
                        self._connection = None

            if self._stop.is_set():
                break
            if progress.received:
                failing_since, delay = None, self._backoff.initial  # the connection worked for a while
            now = time.monotonic()
            if failing_since is None:
                failing_since = now
            if now - failing_since >= self._backoff.give_up_after:
                raise Unreachable(f"lost the connection to the backend: {error}") from error
            log.info("event stream interrupted (%s), reconnecting in %.1fs", error, delay)
            self._interrupted = True
            if self._on_interrupted is not None:
                self._on_interrupted(error, delay)
            if self._sleep(delay):
                break
            delay = min(delay * self._backoff.factor, self._backoff.maximum)
        self.outcome = OUTCOME_STOPPED

    # --- bookkeeping ------------------------------------------------------------

    def _seen(self, event: StreamEvent) -> bool:
        if event.generation_id is None or event.sequence is None:
            return False
        return event.sequence <= self._delivered.get(event.generation_id, -1)

    def _remember(self, event: StreamEvent) -> None:
        self.last_event = event
        if event.generation_id is not None and event.sequence is not None:
            self._delivered[event.generation_id] = event.sequence
            self._last_event_id = event.event_id or f"{event.generation_id}:{event.sequence}"
        elif event.event_id:
            self._last_event_id = event.event_id


class _Progress:
    """Remembers whether any bytes arrived on a connection. A keepalive comment counts."""

    def __init__(self) -> None:
        self.received = False

    def count(self, chunks: Iterator[bytes]) -> Iterator[bytes]:
        for chunk in chunks:
            if chunk:
                self.received = True
            yield chunk
