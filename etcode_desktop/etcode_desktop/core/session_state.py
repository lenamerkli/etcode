"""Everything a view needs to know about one open session, as plain data (no Qt).

:class:`SessionState` turns the controller's events into the transcript, the session's status and
cost, and a few *notices* (a lost connection, a failed load, ...). The Qt layer only has to hand
events to :meth:`SessionState.apply` on the GUI thread, and to draw what changes. That keeps the
rules testable without a display.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import controller as c
from . import models as m
from .attachments import Attachment
from .models import SessionMeta
from .transcript import TranscriptModel

# notice kinds
NOTICE_LOAD = "load_failed"  # the history could not be loaded: offer a retry
NOTICE_CONNECTION = "connection"  # the stream is down and being retried
NOTICE_UNAVAILABLE = "unavailable"  # token rejected, or the session is gone: nothing works
NOTICE_TOOLS = "tools"  # the model asked for tools, and this session cannot run them
NOTICE_ERROR = "error"  # something the user asked for failed (dismissible)

TOOLS_NOTICE = "The model asked for tools, which cannot be run here. Press Stop to cancel the tool calls."


@dataclass(frozen=True)
class Notice:
    kind: str
    text: str
    retry_in: float | None = None  # for a lost connection: seconds until the next attempt
    missing: bool = False  # for "unavailable": the backend does not have the session any more


class SessionState:
    def __init__(self) -> None:
        self.model = TranscriptModel()
        self.meta: SessionMeta | None = None
        self.cost_total: str | None = None  # the exact decimal string
        self.unpriced_generations = 0  # > 0: the cost is only a lower bound
        self.loaded = False  # the history was shown at least once
        self.notices: dict[str, Notice] = {}
        self._listeners: list[Callable[[], None]] = []

    def subscribe(self, listener: Callable[[], None]) -> None:
        """``listener()`` is called after anything but the transcript changed (status, cost, notices)."""
        self._listeners.append(listener)

    def _changed(self) -> None:
        for listener in list(self._listeners):
            listener()

    # --- derived ------------------------------------------------------------------------------------

    @property
    def status(self) -> str | None:
        return self.meta.status if self.meta is not None else None

    @property
    def busy(self) -> bool:
        """The backend is working for this session: a new message would be queued, and Stop makes sense."""
        return self.status in (m.STATUS_GENERATING, m.STATUS_AWAITING_TOOLS)

    @property
    def can_send(self) -> bool:
        notice = self.notices.get(NOTICE_UNAVAILABLE)
        return notice is None

    @property
    def can_continue(self) -> bool:
        """The last answer ended in an error and nothing is running: the user can ask the model to carry on."""
        if not self.loaded or self.busy or not self.can_send:
            return False
        entries = self.model.entries()
        last = entries[-1] if entries else None
        return bool(last is not None and last.role == "assistant" and not last.is_live and last.status == "error")

    @property
    def queued_count(self) -> int:
        return self.meta.queued_count if self.meta is not None else 0

    # --- the user's side ----------------------------------------------------------------------------------

    def begin_send(self, text: str, image_hashes: Sequence[str] = (), attachments: Sequence[Attachment] = ()) -> str:
        """Show a message as 'sending' and return its key, which the controller's ``send`` takes."""
        return self.model.add_pending_user(m.user_message(text, image_hashes)["content"], attachments)

    def dismiss(self, kind: str) -> None:
        if self.notices.pop(kind, None) is not None:
            self._changed()

    # --- events -------------------------------------------------------------------------------------------------

    def apply(self, event: c.Event) -> None:
        model = self.model
        if isinstance(event, c.HistoryLoaded):
            model.load(event.entries)
            self.loaded = True
            self._set_meta(event.meta)
            self.notices.pop(NOTICE_LOAD, None)
            self.notices.pop(NOTICE_UNAVAILABLE, None)  # the backend answered: it works again
            self._changed()
        elif isinstance(event, c.Committed):
            model.apply_committed(event.index, event.message, event.generation_id)
        elif isinstance(event, c.LiveText):
            model.live_text(event.generation_id, event.text)
        elif isinstance(event, c.LiveReasoning):
            model.live_reasoning(event.generation_id, event.text)
        elif isinstance(event, c.LiveToolCall):
            model.live_tool_call(event.generation_id, event.call_id, event.name, event.arguments)
        elif isinstance(event, c.LiveToolResult):
            model.set_tool_result(event.call_id, event.content)
        elif isinstance(event, c.ToolStarted):
            model.set_tool_running(event.call_id)
        elif isinstance(event, c.ToolFinished):
            model.set_tool_result(event.call_id, event.content, event.duration)
        elif isinstance(event, c.UsageReported):
            if event.session_cost_total is not None:
                self.cost_total = event.session_cost_total
                self._changed()
        elif isinstance(event, c.StatusChanged):
            self._set_meta(event.meta)
            if event.meta.status != m.STATUS_AWAITING_TOOLS:
                self.notices.pop(NOTICE_TOOLS, None)
            self._changed()
        elif isinstance(event, c.UserSent):
            model.mark_sent(event.local_key, event.index, event.queued)
        elif isinstance(event, c.ImagesUploaded):
            model.set_uploaded(event.local_key, event.hashes)
        elif isinstance(event, c.UserFailed):
            model.mark_failed(event.local_key, event.message, event.unknown_outcome)
        elif isinstance(event, c.UnknownResolved):
            model.resolve_unknown(event.local_key, "the backend did not receive it")
        elif isinstance(event, c.LoadFailed):
            self._notice(Notice(NOTICE_LOAD, event.message))
        elif isinstance(event, c.ConnectionLost):
            self._notice(Notice(NOTICE_CONNECTION, event.message, event.retry_in))
        elif isinstance(event, c.ConnectionRestored):
            self.dismiss(NOTICE_CONNECTION)
        elif isinstance(event, c.Unavailable):
            self._notice(Notice(NOTICE_UNAVAILABLE, event.message, missing=event.missing))
        elif isinstance(event, c.ToolsPending):
            self._notice(Notice(NOTICE_TOOLS, TOOLS_NOTICE))
        elif isinstance(event, c.ActionFailed):
            self._notice(Notice(NOTICE_ERROR, event.message))
        # GenerationFailed, GenerationCancelled, GenerationEnded and StoreUpdated need nothing here:
        # the committed assistant message carries the error or the cancellation, and the status comes
        # with StatusChanged.

    def _set_meta(self, meta: SessionMeta) -> None:
        self.meta = meta
        self.cost_total = meta.cost.total_exact
        self.unpriced_generations = meta.cost.unpriced_generations

    def _notice(self, notice: Notice) -> None:
        self.notices[notice.kind] = notice
        self._changed()

    def cost_text(self) -> str:
        """'$0.0123', or '≥ $0.0123 (2 unpriced)' when some generations could not be priced. Empty if unknown."""
        if self.cost_total is None:
            return ""
        text = f"${self.cost_total}"
        if self.unpriced_generations:
            return f"≥ {text} ({self.unpriced_generations} unpriced)"
        return text

