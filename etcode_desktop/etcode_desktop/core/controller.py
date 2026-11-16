"""Drives one session: loads its history, sends messages, follows what the backend streams, stops it.

The controller keeps no history of its own. The backend is the source of truth. The controller
only turns what it learns into *events* (the dataclasses below), which a view applies to a
:class:`~etcode_desktop.core.transcript.TranscriptModel`. Events are emitted from worker threads.
The callback must hand them over to the GUI thread.

Threads (all daemon threads, so that quitting never waits for the network):

* a short one for each command (``open``, ``send``, ``stop``, ``refresh``)
* at most one *follower* thread, which reads the event streams of the session's generations one
  after the other until the backend has nothing more to do for it

After every generation the follower asks ``GET /session/<id>`` what comes next, instead of
guessing from events: a queued message or a backend-run web search starts another generation
(``done.next_generation_id``), tool calls wait for results, and otherwise the session is idle.

Tool calls
----------
When the model asks for tools, the session waits (``awaiting_tools``) until every call has a result. If the
controller has a :class:`~etcode_desktop.core.tools.scheduler.ToolRunner`, it runs the calls that the client is
responsible for (not ``websearch``, which the backend runs) side by side, and posts each result as soon as it is
there. For each call the journal in the local store says what was done, so that a restart never runs a command
twice: a result that was not posted yet is posted again, and a call that was running when the app died is
answered with a note instead of being run again (a shell command is not safe to repeat).

Without a runner, the calls are only reported with :class:`ToolsPending`, and ``stop()`` cancels them.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import models as m
from .attachments import Attachment
from .api import BackendClient
from .describe import describe_error
from .errors import AuthError, BackendError, Conflict, NotFound, ProtocolError, ServerError, Unreachable
from .models import HistoryEntry, PendingToolCall, SessionMeta, StreamEvent
from .sse import OUTCOME_NO_GENERATION, Backoff, EventFollower
from .store import JOURNAL_POSTED, JournalEntry, Scope, SessionStore, StoreError
from .tools.cancel import CancelToken

if TYPE_CHECKING:
    from .tools.scheduler import BatchCall, ToolRunner

log = logging.getLogger(__name__)

INTERRUPTED_TEXT = ("Error: the desktop client was interrupted while executing this tool call. The outcome is unknown. "
                    "Check the current state before retrying.")
MAX_RESULT_CHARS = 1_900_000  # the backend accepts 2 000 000 characters of text in one request
STOP_WAIT_SECONDS = 10.0  # how long the follower waits for a Stop to reach the backend before it looks again
MAX_UNEXPECTED_FAILURES = 3  # in a row, in the follower, before it gives up on a session
VERIFY_GRACE_SECONDS = 3.0  # a message whose outcome is unknown may still be on its way: look in the history after this long
VERIFY_ATTEMPTS = 20  # tries to reach the backend for that

RETRY_DELAYS = (3.0, 5.0, 10.0, 20.0, 30.0)  # waits between attempts after the connection was lost for good
POLL_DELAYS = (0.3, 1.0, 2.0)  # waits while the backend says "generating" but has no stream for us yet
HISTORY_TAIL_LIMIT = 1000  # what GET /session/<id>/last/<n> allows


# --- events ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class HistoryLoaded:
    entries: tuple[HistoryEntry, ...]
    meta: SessionMeta


@dataclass(frozen=True)
class Committed:
    """A message that now has its permanent index."""
    index: int
    message: dict[str, Any]
    generation_id: str | None = None  # set if it is the final version of a message that was streamed


@dataclass(frozen=True)
class LiveText:
    generation_id: str
    text: str


@dataclass(frozen=True)
class LiveReasoning:
    generation_id: str
    text: str


@dataclass(frozen=True)
class LiveToolCall:
    generation_id: str
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LiveToolResult:
    """The result of a call that the backend ran itself (web search)."""
    call_id: str
    content: list[dict[str, Any]]


@dataclass(frozen=True)
class UsageReported:
    session_cost_total: str | None  # the exact decimal string
    cost: str | None
    currency: str


@dataclass(frozen=True)
class GenerationFailed:
    generation_id: str
    error: dict[str, Any]


@dataclass(frozen=True)
class GenerationCancelled:
    generation_id: str


@dataclass(frozen=True)
class GenerationEnded:
    generation_id: str
    status: str | None
    finish_reason: str | None


@dataclass(frozen=True)
class StatusChanged:
    meta: SessionMeta


@dataclass(frozen=True)
class UserSent:
    local_key: str
    index: int | None  # set if the message was committed at once
    queued: bool


@dataclass(frozen=True)
class ImagesUploaded:
    """The pictures of a message that is being sent are on the backend."""
    local_key: str
    hashes: tuple[str, ...]


@dataclass(frozen=True)
class UnknownResolved:
    """The backend was asked again, and the message whose outcome was unknown is not in its history: it never arrived."""
    local_key: str


@dataclass(frozen=True)
class UserFailed:
    local_key: str
    message: str
    unknown_outcome: bool  # the connection broke: the message may have arrived anyway


@dataclass(frozen=True)
class LoadFailed:
    message: str


@dataclass(frozen=True)
class ConnectionLost:
    message: str
    retry_in: float | None


@dataclass(frozen=True)
class ConnectionRestored:
    pass


@dataclass(frozen=True)
class Unavailable:
    """The session can't be used: the token was rejected, or the backend doesn't have it."""
    message: str
    missing: bool = False


@dataclass(frozen=True)
class ToolsPending:
    """The model wants tools, and this controller cannot run them (it has no runner). The session waits until it is stopped."""
    calls: tuple[PendingToolCall, ...]


@dataclass(frozen=True)
class ToolStarted:
    call_id: str


@dataclass(frozen=True)
class ToolFinished:
    """A tool call has its result (it may not be posted yet)."""
    call_id: str
    content: list[dict[str, Any]]
    duration: float | None  # seconds, None if it was not run now (a result from the journal)


@dataclass(frozen=True)
class ActionFailed:
    message: str


@dataclass(frozen=True)
class StoreUpdated:
    """The session's row in the local store changed (status, cost, title). The sidebar should reload."""


Event = (HistoryLoaded | Committed | LiveText | LiveReasoning | LiveToolCall | LiveToolResult | UsageReported
         | GenerationFailed | GenerationCancelled | GenerationEnded | StatusChanged | UserSent | UserFailed | UnknownResolved | ImagesUploaded
         | LoadFailed | ConnectionLost | ConnectionRestored | Unavailable | ToolsPending | ToolStarted | ToolFinished
         | ActionFailed | StoreUpdated)


class SessionController:
    def __init__(
        self,
        client: BackendClient,
        store: SessionStore,
        scope: Scope,
        session_id: str,
        emit: Callable[[Event], None],
        *,
        backoff: Backoff = Backoff(),
        retry_delays: Sequence[float] = RETRY_DELAYS,
        poll_delays: Sequence[float] = POLL_DELAYS,
        tools: ToolRunner | None = None,
        verify_grace: float = VERIFY_GRACE_SECONDS,
        verify_attempts: int = VERIFY_ATTEMPTS,
    ):
        self.session_id = session_id
        self._client = client
        self._store = store
        self._scope = scope
        self._emit_callback = emit
        self._backoff = backoff
        self._retry_delays = tuple(retry_delays)
        self._poll_delays = tuple(poll_delays)
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._follow_thread: threading.Thread | None = None
        self._rescan = False  # something asked to follow again while the follower was finishing
        self._follower: EventFollower | None = None
        self._last_event_id: str | None = None  # to continue a generation after the connection was lost
        self._seen: set[int] = set()  # message indexes that were reported
        self._connection_lost = False
        self._poll_count = 0
        self._tools = tools
        self._cancel = CancelToken()  # for the tools that run now. Stop replaces it with a fresh one
        self._inflight: dict[str, Future] = {}
        self._legacy_calls: dict[str, PendingToolCall] = {}  # pending calls, for a backend that does not list them
        self._verify_grace = verify_grace
        self._verify_attempts = verify_attempts
        self._wake = threading.Event()  # set by retry_now() and close(): ends a wait before the next attempt
        self._needs_reopen = False  # the history was not loaded, or the session was unavailable: loading it again may work now
        self._last_round: tuple[str, ...] | None = None  # the calls of the last round of tools, to notice a round that repeats
        self._stopping = False
        self._stop_finished = threading.Event()
        self._stop_finished.set()

    # --- plumbing -------------------------------------------------------------------------------------

    def _emit(self, event: Event) -> None:
        if self._closed.is_set():
            return
        try:
            self._emit_callback(event)
        except Exception:  # a broken view must not kill the thread that feeds it
            log.exception("handling %s failed", type(event).__name__)

    def _spawn(self, name: str, target: Callable[[], None]) -> threading.Thread:
        thread = threading.Thread(target=self._guard(target), daemon=True, name=f"{name}-{self.session_id[:6]}")
        thread.start()
        return thread

    def _guard(self, target: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                target()
            except Exception as exc:
                if self._closed.is_set():
                    log.debug("a controller thread ended with an error while closing: %s", exc)  # expected: the world is going away
                    return
                log.exception("session controller thread failed")
                self._emit(ActionFailed(f"Unexpected error: {describe_error(exc)}"))
        return run

    def _wait(self, seconds: float) -> bool:
        """Sleep. Returns True if the controller was closed meanwhile."""
        return self._closed.wait(seconds)

    def _wait_retry(self, seconds: float) -> bool:
        """Sleep before another attempt, unless :meth:`retry_now` comes first. Returns True if closed meanwhile."""
        self._wake.wait(seconds)
        self._wake.clear()
        return self._closed.is_set()

    @property
    def is_following(self) -> bool:
        with self._lock:
            return self._follow_thread is not None and self._follow_thread.is_alive()

    # --- commands ---------------------------------------------------------------------------------------

    def open(self) -> None:
        """Load the history and continue whatever the backend is doing for this session."""
        self._spawn("open", self._open)

    def refresh(self) -> None:
        """Ask again (after a failed load, or when the user wants an update)."""
        self._spawn("refresh", self._open)

    def retry_now(self) -> None:
        """Stop waiting for the next attempt to reach the backend, and try at once."""
        self._wake.set()
        with self._lock:
            follower = self._follower
        if follower is not None:
            follower.retry_now()

    def recover(self) -> None:
        """The backend is known to answer again: pick up whatever was stuck (a history that did not load, a
        session that was refused, a connection that waits for its next attempt)."""
        self.retry_now()
        if self._needs_reopen:
            self.refresh()

    def send(self, local_key: str, text: str, image_hashes: Sequence[str] = (), attachments: Sequence[Attachment] = (),
             **overrides: Any) -> None:
        """Send a user message. ``local_key`` names the pending entry in the view. ``image_hashes`` are pictures that are on the
        backend already, ``attachments`` are pictures that are uploaded first. ``overrides`` are temperature, max_tokens and
        reasoning_effort."""
        self._spawn("send", lambda: self._send(local_key, text, tuple(image_hashes), tuple(attachments), overrides))

    def change_model(self, tech_name: str) -> None:
        """Use another model for the rest of the session (the backend allows it while the session is idle or failed,
        and only between models that both do, or both do not, take images)."""
        self._spawn("model", lambda: self._change_model(tech_name))

    def stop(self) -> None:
        """Cancel the running generation, or give up on tool calls that wait for results.

        Tools that are running are ended at once (a shell command and everything it started)."""
        self._stopping = True
        self._stop_finished.clear()
        self._cancel_tools()
        self._spawn("stop", self._stop_and_finish)

    def _cancel_tools(self) -> None:
        old, self._cancel = self._cancel, CancelToken()  # calls that start later get a token that is not cancelled
        old.cancel()

    def close(self, wait: float = 0.0) -> None:
        """Stop following and go quiet: no event is emitted any more. With ``wait``, give the follower
        thread that many seconds to finish (closing its connection ends it almost at once)."""
        self._closed.set()
        self._wake.set()
        self._cancel.cancel()  # a shell command must not outlive the window that started it
        with self._lock:
            follower = self._follower
            thread = self._follow_thread
        if follower is not None:
            follower.close()
        if wait > 0 and thread is not None and thread is not threading.current_thread():
            thread.join(wait)

    # --- opening --------------------------------------------------------------------------------------------

    def _open(self) -> None:
        try:
            meta = self._client.get_session(self.session_id)
            entries = self._client.messages(self.session_id)
        except (AuthError, NotFound) as exc:
            self._unavailable(exc)
            return
        except (Unreachable, ServerError, ProtocolError, BackendError) as exc:
            self._needs_reopen = True
            self._emit(LoadFailed(describe_error(exc)))
            return
        self._needs_reopen = False
        self._last_event_id = None  # the view starts again from the history: a running generation is replayed from its start
        with self._lock:
            self._seen = {e.index for e in entries}
        if meta.pending_tool_calls is None:
            self._legacy_calls = _calls_without_results(entries)
        self._publish_meta(meta)
        self._emit(HistoryLoaded(tuple(entries), meta))
        if entries:
            self._store_last_seen(max(e.index for e in entries))
        if meta.status in (m.STATUS_GENERATING, m.STATUS_AWAITING_TOOLS):
            self._ensure_following()

    def _unavailable(self, exc: BackendError) -> None:
        self._needs_reopen = True
        missing = isinstance(exc, NotFound)
        if missing:
            self._store.mark_missing(self._scope, self.session_id)
            self._emit(StoreUpdated())
        self._emit(Unavailable("This session is not on the backend any more." if missing else describe_error(exc), missing))

    # --- sending -----------------------------------------------------------------------------------------------

    def _change_model(self, tech_name: str) -> None:
        try:
            meta = self._client.set_model(self.session_id, tech_name)
        except (AuthError, NotFound) as exc:
            self._unavailable(exc)
            return
        except (Unreachable, BackendError, ProtocolError) as exc:
            self._emit(ActionFailed(f"The model could not be changed: {describe_error(exc)}"))
            self._publish_after_failed_change()
            return
        self._publish_meta(meta)

    def _publish_after_failed_change(self) -> None:
        try:
            self._publish_meta(self._client.get_session(self.session_id))  # so that the view shows the model that is really used
        except (Unreachable, BackendError, ProtocolError):
            pass

    def _send(self, local_key: str, text: str, image_hashes: tuple[str, ...], attachments: tuple[Attachment, ...],
              overrides: dict[str, Any]) -> None:
        if attachments:
            hashes = list(image_hashes)
            try:
                for attachment in attachments:
                    hashes.append(self._client.upload_image(attachment.data, attachment.name).hash)
            except (AuthError, NotFound) as exc:
                self._emit(UserFailed(local_key, describe_error(exc), unknown_outcome=False))
                self._unavailable(exc)
                return
            except (Unreachable, BackendError, ProtocolError) as exc:
                # nothing was sent yet: the message is known not to have arrived, and can be sent again
                self._emit(UserFailed(local_key, f"the picture could not be uploaded: {describe_error(exc)}", unknown_outcome=False))
                return
            image_hashes = tuple(hashes)
            self._emit(ImagesUploaded(local_key, image_hashes))
        try:
            result = self._client.submit(self.session_id, [m.user_message(text, image_hashes)], **overrides)
        except (Unreachable, ServerError) as exc:
            self._emit(UserFailed(local_key, describe_error(exc), unknown_outcome=True))
            self._spawn("verify", lambda: self._verify_unknown(local_key))
            return
        except BackendError as exc:
            self._emit(UserFailed(local_key, describe_error(exc), unknown_outcome=False))
            if isinstance(exc, (NotFound, AuthError)):  # the whole session is the problem, not this message
                self._unavailable(exc)
            return
        accepted = result.accepted[0] if result.accepted else {}
        index = accepted.get("index") if isinstance(accepted.get("index"), int) else None
        if index is not None:
            self._mark_seen(index)
        if text.strip():
            self._store.set_title_if_unset(self._scope, self.session_id, text)
        self._emit(UserSent(local_key, index, bool(accepted.get("queued"))))
        self._publish_meta(result.session)
        if result.generation_id or result.session.status != m.STATUS_IDLE:
            self._ensure_following()

    def _verify_unknown(self, local_key: str) -> None:
        """A message was sent, and the answer was lost. Once the backend answers again, its history says whether
        the message arrived: if it did, it is shown as committed (the sync reports it). If not, say so."""
        if self._wait(self._verify_grace):  # the request may still be on its way to the backend
            return
        for attempt in range(self._verify_attempts):
            try:
                self._refresh_status()
            except (Unreachable, ServerError, ProtocolError):
                if self._wait_retry(self._retry_delays[min(attempt, len(self._retry_delays) - 1)]):
                    return
                continue
            except (AuthError, NotFound) as exc:
                self._unavailable(exc)  # the session is gone or refused: say so. The message stays "not confirmed"
                return
            except BackendError:
                return
            self._emit(UnknownResolved(local_key))
            return

    # --- stopping ---------------------------------------------------------------------------------------------------

    def _stop_and_finish(self) -> None:
        try:
            self._stop()
        finally:
            self._stopping = False
            self._stop_finished.set()

    def _stop(self) -> None:
        result: dict[str, Any] = {}
        try:
            result = self._client.cancel(self.session_id)
        except Conflict:
            pass  # nothing to cancel: it finished meanwhile
        except (Unreachable, BackendError) as exc:
            self._emit(ActionFailed(f"Could not stop: {describe_error(exc)}"))
            return
        try:
            meta = self._refresh_status()
        except (Unreachable, BackendError, ProtocolError):
            return
        if result.get("generation_id") or meta.status in (m.STATUS_GENERATING, m.STATUS_AWAITING_TOOLS):
            self._ensure_following()

    # --- following ------------------------------------------------------------------------------------------------------

    def _ensure_following(self) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            if self._follow_thread is not None and self._follow_thread.is_alive():
                self._rescan = True  # it may be about to finish: make it look once more
                return
            self._follow_thread = self._spawn("follow", self._follow_loop)

    def _follow_loop(self) -> None:
        attempt = 0
        unexpected = 0
        try:
            while not self._closed.is_set():
                try:
                    again = self._follow_once()
                except (Unreachable, ServerError, ProtocolError) as exc:
                    delay = self._retry_delays[min(attempt, len(self._retry_delays) - 1)]
                    attempt += 1
                    self._connection_lost = True
                    self._emit(ConnectionLost(describe_error(exc), delay))
                    if self._wait_retry(delay):
                        break
                    continue
                except BackendError as exc:
                    self._unavailable(exc)
                    break
                except Exception as exc:  # a bug, or something odd: try again a few times before giving up on the session
                    unexpected += 1
                    log.exception("following the session %s failed (%d in a row)", self.session_id, unexpected)
                    if unexpected >= MAX_UNEXPECTED_FAILURES:
                        self._emit(ActionFailed(f"This session stopped updating because of an unexpected error: {describe_error(exc)}. "
                                                "Close it and open it again to continue."))
                        break
                    if self._wait_retry(self._retry_delays[0]):
                        break
                    continue
                unexpected = 0
                attempt = 0
                if self._connection_lost:
                    self._connection_lost = False
                    self._emit(ConnectionRestored())
                if again:
                    continue
                with self._lock:
                    if self._rescan:
                        self._rescan = False
                        continue
                    self._follow_thread = None
                    return
        finally:
            with self._lock:
                if self._follow_thread is threading.current_thread():
                    self._follow_thread = None

    def _follow_once(self) -> bool:
        """Follow one generation to its end. Returns True if there is more to follow."""
        follower = EventFollower(
            self._client, self.session_id, since=self._last_event_id, backoff=self._backoff,
            on_interrupted=lambda exc, delay: self._emit(ConnectionLost(describe_error(exc), delay)),
            on_resumed=lambda: self._emit(ConnectionRestored()),
        )
        with self._lock:
            if self._closed.is_set():
                return False
            self._follower = follower
        done: StreamEvent | None = None
        try:
            for event in follower:
                self._handle(event)
                if event.is_done:
                    done = event
        finally:
            with self._lock:
                self._follower = None
            # keep the position if the generation may continue (the connection was given up), else start fresh
            self._last_event_id = follower.last_event_id if follower.outcome is None else None
        if self._closed.is_set():
            return False

        meta = self._refresh_status()
        if done is not None and done.next_generation_id:
            return True
        if meta.status == m.STATUS_GENERATING:
            self._wait(self._poll_delay(follower.outcome == OUTCOME_NO_GENERATION))
            return not self._closed.is_set()
        if meta.status == m.STATUS_AWAITING_TOOLS:
            return self._tools_phase(meta)
        self._poll_count = 0
        self._last_round = None
        self._legacy_calls.clear()
        return False

    # --- running tools ------------------------------------------------------------------------------------------------------

    def _calls_to_run(self, meta: SessionMeta) -> list[PendingToolCall]:
        """The calls that wait for a result from this client, in the order of the model."""
        if meta.pending_tool_calls is not None:
            return list(meta.unresolved_client_calls())
        # An older backend does not say which calls wait. The stream and the history say which calls were made,
        # and the journal says which of them we have answered.
        calls = []
        for call in self._legacy_calls.values():
            entry = self._store.journal_get(self._scope, self.session_id, call.id)
            if call.name != "websearch" and not (entry is not None and entry.state == JOURNAL_POSTED):
                calls.append(call)
        return calls

    def _tools_phase(self, meta: SessionMeta) -> bool:
        """The session waits for tool results. Returns True if there is more to follow afterwards."""
        calls = self._calls_to_run(meta)
        if self._tools is None:
            self._emit(ToolsPending(tuple(calls)))
            return False
        if self._stopping:  # let the Stop reach the backend first: then these calls are resolved, and nothing is left to run
            self._stop_finished.wait(STOP_WAIT_SECONDS)
            return not self._closed.is_set()
        if not calls:
            self._wait(self._poll_delays[-1])  # only the backend's own web search is left: it resumes by itself
            return not self._closed.is_set()
        round_ids = tuple(call.id for call in calls)
        if round_ids == self._last_round:
            # The same calls again: the backend still waits although we answered. Do not ask it at the speed of the network.
            self._wait(self._poll_delay(True))
        self._last_round = round_ids
        self._execute_calls(calls)
        return not self._closed.is_set()

    def _execute_calls(self, calls: Sequence[PendingToolCall]) -> None:
        """Start the calls side by side and wait until each of them is posted (or stopped)."""
        assert self._tools is not None
        from .tools.scheduler import BatchCall
        token = self._cancel
        batch: list[BatchCall] = []
        plans: dict[str, tuple[str, JournalEntry | None]] = {}
        for call in calls:
            plan = self._plan(call)
            if plan is not None:
                plans[call.id] = plan
                batch.append(BatchCall(call.id, call.name, call.arguments))
        if not batch:
            self._wait(self._poll_delays[0])
            return
        futures = self._tools.submit_batch(batch, lambda call: self._run_call(call, plans[call.id], token))
        with self._lock:
            for call, future in zip(batch, futures):
                self._inflight[call.id] = future
        try:
            self._await(futures, token)
        finally:
            with self._lock:
                for call in batch:
                    self._inflight.pop(call.id, None)
        failure = next((f.exception() for f in futures if f.exception() is not None), None)
        if failure is not None:
            raise failure

    def _await(self, futures: Sequence[Future], token: CancelToken) -> None:
        while True:
            _, pending = wait(futures, timeout=0.1)
            if not pending:
                return
            if self._closed.is_set():
                token.cancel()  # the commands end at once, and the calls with them

    def _plan(self, call: PendingToolCall) -> tuple[str, JournalEntry | None] | None:
        """What to do with a call that waits for a result: 'start' it, 'resend' its journaled result, or answer 'interrupted'."""
        with self._lock:
            if call.id in self._inflight:
                return None  # it is being run
        entry = self._store.journal_get(self._scope, self.session_id, call.id)
        if entry is None:
            return "start", None
        if entry.result is not None:
            return "resend", entry
        return "interrupted", entry  # 'running' in the journal, but not in this process: the app died while it ran

    def _run_call(self, call: BatchCall, plan: tuple[str, JournalEntry | None], token: CancelToken) -> None:
        """The whole life of one call, on a pool thread: run (or recall), journal, tell the view, post."""
        assert self._tools is not None
        kind, entry = plan
        started = time.monotonic()
        duration: float | None = None
        if kind == "start":
            try:
                if not self._store.journal_start(self._scope, self.session_id, call.id, call.name, call.arguments):
                    return  # somebody else has taken it
            except (sqlite3.Error, StoreError, OSError) as exc:
                # Without the journal a crash could make the command run twice. So it is not run. The model is told,
                # which also keeps the session from waiting for a result that never comes.
                log.error("cannot write the tool journal: %s", exc)
                content = [m.text_part(f"Error: the desktop client could not write its journal, so the command was not run ({exc}).")]
                self._emit(ToolFinished(call.id, content, None))
                if not token.cancelled:
                    self._post_result(call.id, content)
                return
            self._emit(ToolStarted(call.id))
            content = _limit(self._tools.run(call.name, call.arguments, self._client, token))
            duration = time.monotonic() - started
            self._journal(self._store.journal_done, call.id, content)
        elif kind == "interrupted":
            content = [m.text_part(INTERRUPTED_TEXT)]
            self._journal(self._store.journal_done, call.id, content)
        else:
            assert entry is not None and entry.result is not None
            content = entry.result
        self._emit(ToolFinished(call.id, content, duration))
        if token.cancelled:
            return  # Stop: the backend resolves this call itself, and a result now could only start another generation
        self._post_result(call.id, content)
        self._journal(self._store.journal_posted, call.id)

    def _journal(self, write: Callable[..., None], call_id: str, *args: Any) -> None:
        """A journal entry after the fact. If it cannot be written, the call has still been done: say so in the log, go on."""
        try:
            write(self._scope, self.session_id, call_id, *args)
        except (sqlite3.Error, StoreError, OSError) as exc:
            log.error("cannot update the tool journal for %s: %s", call_id, exc)

    def _post_result(self, call_id: str, content: list[dict[str, Any]]) -> None:
        try:
            result = self._client.submit(self.session_id, [m.tool_message(call_id, content)])
        except Conflict:
            return  # already resolved (by Stop, say), or answered before: nothing to do
        self._publish_meta(result.session)

    def _poll_delay(self, no_stream: bool) -> float:
        # The backend says "generating" but there was no stream (yet): look again soon, then less often.
        delay = self._poll_delays[min(self._poll_count, len(self._poll_delays) - 1)] if no_stream else self._poll_delays[0]
        self._poll_count += 1
        return delay

    def _handle(self, event: StreamEvent) -> None:
        data, generation = event.data, event.generation_id or ""
        try:
            if event.kind == m.TEXT_DELTA:
                self._emit(LiveText(generation, str(data.get("text", ""))))
            elif event.kind == m.REASONING_DELTA:
                self._emit(LiveReasoning(generation, str(data.get("text", ""))))
            elif event.kind == m.TOOL_CALL:
                call = PendingToolCall(str(data["tool_call_id"]), str(data["name"]), dict(data.get("arguments") or {}), False)
                self._legacy_calls[call.id] = call
                self._emit(LiveToolCall(generation, call.id, call.name, call.arguments))
            elif event.kind == m.TOOL_RESULT:
                self._emit(LiveToolResult(str(data["tool_call_id"]), list(data.get("content") or [])))
            elif event.kind == m.USAGE:
                self._emit(UsageReported(_text_or_none(data.get("session_cost_total")), _text_or_none(data.get("cost")),
                                         str(data.get("currency", "USD"))))
            elif event.kind == m.MESSAGE_COMPLETE:
                index = data["index"]
                self._mark_seen(index)
                self._emit(Committed(index, dict(data["message"]), generation or None))
            elif event.kind == m.ERROR:
                self._emit(GenerationFailed(generation, dict(data)))
            elif event.kind == m.CANCELLED:
                self._emit(GenerationCancelled(generation))
            elif event.kind == m.DONE:
                self._emit(GenerationEnded(generation, data.get("status"), data.get("finish_reason")))
        except (KeyError, TypeError, ValueError):
            log.warning("ignoring a malformed %r event", event.kind)

    # --- status and history ------------------------------------------------------------------------------------------------

    def _refresh_status(self) -> SessionMeta:
        meta = self._client.get_session(self.session_id)
        self._publish_meta(meta)
        self._sync_history(meta)
        return meta

    def _publish_meta(self, meta: SessionMeta) -> None:
        try:
            self._store.upsert_from_meta(self._scope, meta)
        except Exception:  # the local list is a convenience: never let it break the conversation
            log.exception("could not update the stored session %s", self.session_id)
        self._emit(StatusChanged(meta))
        self._emit(StoreUpdated())

    def _mark_seen(self, index: int) -> None:
        with self._lock:
            self._seen.add(index)
        self._store_last_seen(index)

    def _store_last_seen(self, index: int) -> None:
        try:
            self._store.advance_last_seen(self._scope, self.session_id, index)
        except Exception:
            log.exception("could not store the last seen message")

    def _sync_history(self, meta: SessionMeta) -> None:
        """Fetch committed messages that were not reported yet (a queued user message, results of
        cancelled tool calls, whatever happened while the stream was down)."""
        with self._lock:
            seen = set(self._seen)
        missing = [i for i in range(meta.message_count) if i not in seen]
        if not missing:
            return
        count = meta.message_count - missing[0]
        entries = (self._client.last(self.session_id, count) if count <= HISTORY_TAIL_LIMIT
                   else self._client.messages(self.session_id))
        for entry in sorted(entries, key=lambda e: e.index):
            if entry.index in seen:
                continue
            self._mark_seen(entry.index)
            self._emit(Committed(entry.index, entry.message, None))


def _text_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _limit(content: list[dict[str, Any]], limit: int = MAX_RESULT_CHARS) -> list[dict[str, Any]]:
    """Keep the text of a tool result below what the backend accepts. A result is never empty."""
    remaining, kept = limit, []
    for part in content:
        if part.get("type") == "text":
            text = part.get("text", "")
            if len(text) > remaining:
                text = text[:max(remaining, 0)] + f"\n[result truncated: {len(part.get('text', '')) - max(remaining, 0)} more characters]"
            remaining -= len(part.get("text", ""))
            part = m.text_part(text)
        kept.append(part)
    return kept or [m.text_part("(no output)")]


def _calls_without_results(entries: Sequence[HistoryEntry]) -> dict[str, PendingToolCall]:
    """For a backend that does not list pending calls: the calls of the last assistant message that have no result yet."""
    last_calls: dict[str, PendingToolCall] = {}
    answered: set[str] = set()
    for entry in sorted(entries, key=lambda e: e.index):
        message = entry.message
        if message.get("role") == "assistant":
            last_calls = {c["id"]: PendingToolCall(c["id"], c["name"], dict(c.get("arguments") or {}), False)
                          for c in message.get("tool_calls") or [] if isinstance(c, dict) and "id" in c and "name" in c}
            answered = set()
        elif message.get("role") == "tool":
            answered.add(message.get("tool_call_id", ""))
    return {i: c for i, c in last_calls.items() if i not in answered and c.name != "websearch"}
