"""What the conversation view shows, as a plain data model (no Qt).

Two sources feed it:

* **committed messages** from the backend (history, ``message_complete`` events), each with
  a permanent index. Applying the same index again changes nothing.
* **live things** that have no index yet: the assistant message that is being streamed, and
  user messages that were sent but not committed yet (sending, queued, or failed).

The model keeps the committed entries sorted by index, and the live ones after them in the
order they appeared. Views follow it through :meth:`TranscriptModel.subscribe`.

Mutate it from one thread only (the GUI thread); it has no locks.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .attachments import Attachment
from .models import HistoryEntry

# user entry states
COMMITTED = "committed"
SENDING = "sending"  # the request is on its way
QUEUED = "queued"  # the backend accepted it, and will commit it after the current work
FAILED = "failed"  # the backend refused it: it was not sent
UNKNOWN = "unknown"  # the connection broke: it may or may not have arrived

# assistant entry states
STREAMING = "streaming"

# change kinds
ADDED, UPDATED, REMOVED, RESET = "added", "updated", "removed", "reset"


@dataclass
class ToolCallView:
    id: str
    name: str
    arguments: dict[str, Any]
    result: list[dict[str, Any]] | None = None  # content parts of the tool result, once known
    running: bool = False  # this client is running it now
    duration: float | None = None  # seconds, if it was run here and now


@dataclass
class UserEntry:
    key: str
    content: list[dict[str, Any]]
    index: int | None = None
    state: str = COMMITTED
    error: str | None = None
    attachments: list[Attachment] = field(default_factory=list)  # the pictures as the user attached them, while the message is pending
    role = "user"

    @property
    def text(self) -> str:
        return "\n".join(p.get("text", "") for p in self.content if p.get("type") == "text")

    @property
    def image_hashes(self) -> list[str]:
        return [p["hash"] for p in self.content if p.get("type") == "image" and "hash" in p]


@dataclass
class AssistantEntry:
    key: str
    generation_id: str | None = None
    index: int | None = None
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCallView] = field(default_factory=list)
    status: str = STREAMING  # streaming, complete, cancelled or error
    error: dict[str, Any] | None = None
    role = "assistant"

    @property
    def is_live(self) -> bool:
        return self.index is None


Entry = UserEntry | AssistantEntry


@dataclass(frozen=True)
class Change:
    kind: str
    key: str | None = None


class TranscriptModel:
    def __init__(self) -> None:
        self._committed: dict[int, Entry] = {}
        self._tail: list[Entry] = []
        self._seen: set[int] = set()  # every index applied, including the system prompt and tool messages
        self._calls: dict[str, tuple[ToolCallView, AssistantEntry]] = {}
        self._orphan_results: dict[str, list[dict[str, Any]]] = {}  # a tool result that came before its call
        self._listeners: list[Callable[[Change], None]] = []
        self._counter = 0

    # --- reading -------------------------------------------------------------------------

    def entries(self) -> list[Entry]:
        return [self._committed[i] for i in sorted(self._committed)] + self._tail

    def position(self, key: str) -> int | None:
        for position, entry in enumerate(self.entries()):
            if entry.key == key:
                return position
        return None

    def get(self, key: str) -> Entry | None:
        return next((e for e in self.entries() if e.key == key), None)

    def has_index(self, index: int) -> bool:
        return index in self._seen

    @property
    def seen_indexes(self) -> frozenset[int]:
        return frozenset(self._seen)

    def subscribe(self, listener: Callable[[Change], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[Change], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, kind: str, key: str | None = None) -> None:
        change = Change(kind, key)
        for listener in list(self._listeners):
            listener(change)

    def _new_key(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    # --- committed messages ------------------------------------------------------------------

    def reset(self) -> None:
        self._committed.clear()
        self._tail.clear()
        self._seen.clear()
        self._calls.clear()
        self._orphan_results.clear()
        self._notify(RESET)

    def load(self, history: Iterable[HistoryEntry]) -> None:
        """Replace what is shown with the backend's history.

        Messages that the user sent and that are not in the history stay (sending, queued, not sent, unknown):
        the backend does not have them, so the history cannot replace them. If the history has a copy of one
        of them, it takes the place of the pending message, as always. A message that was being streamed is
        dropped: the history has it complete, or the stream is replayed from its start.
        """
        pending = [e for e in self._tail if isinstance(e, UserEntry)]
        self.reset()
        self._tail.extend(pending)
        if pending:
            self._notify(RESET)
        for entry in history:
            self.apply_committed(entry.index, entry.message)

    def apply_committed(self, index: int, message: dict[str, Any], generation_id: str | None = None) -> None:
        """Show a committed message. A repeated index is ignored.

        ``generation_id`` says which live assistant message this is the final version of, when known.
        """
        if index in self._seen:
            if generation_id is not None and message.get("role") == "assistant":
                # a replay of a generation whose message is already shown: its live copy is a duplicate
                stale = self._live_assistant(generation_id)
                if stale is not None:
                    self._remove(stale)
            return
        self._seen.add(index)
        role = message.get("role")
        if role == "user":
            self._commit_user(index, message)
        elif role == "assistant":
            self._commit_assistant(index, message, generation_id)
        elif role == "tool":
            self._commit_tool(message)
        # "system" (the prompt at index 0) and anything unknown is not shown

    def _commit_user(self, index: int, message: dict[str, Any]) -> None:
        content = [dict(p) for p in message.get("content") or []]
        pending = next((e for e in self._tail if isinstance(e, UserEntry) and e.state in (SENDING, QUEUED, UNKNOWN)
                        and e.content == content), None)
        if pending is not None:  # the message that we showed as pending is now committed: show it once
            self._remove(pending)
        entry = UserEntry(self._new_key("u"), content, index, COMMITTED)
        self._committed[index] = entry
        self._notify(ADDED, entry.key)

    def _commit_assistant(self, index: int, message: dict[str, Any], generation_id: str | None) -> None:
        text = (message.get("text") or {}).get("text", "")
        reasoning = (message.get("reasoning") or {}).get("text", "")
        if generation_id is not None:
            live = self._live_assistant(generation_id)
        else:
            # A committed message that we met in the history, not in the stream. If a live message that
            # was cut short is its beginning, this is the same message: finish that one.
            live = next((e for e in self._tail if isinstance(e, AssistantEntry)
                         and text.startswith(e.text) and reasoning.startswith(e.reasoning)), None)
        before = self.position(live.key) if live is not None else None
        entry = live if live is not None else AssistantEntry(self._new_key("a"))
        entry.index = index
        entry.text = text
        entry.reasoning = reasoning
        entry.status = message.get("status") or "complete"
        entry.error = message.get("error")
        known = {c.id: c for c in entry.tool_calls}
        entry.tool_calls = []
        for raw in message.get("tool_calls") or []:
            call = known.get(raw.get("id")) or ToolCallView(raw.get("id", ""), raw.get("name", ""), dict(raw.get("arguments") or {}))
            call.name, call.arguments = raw.get("name", call.name), dict(raw.get("arguments") or call.arguments)
            if call.result is None and call.id in self._orphan_results:
                call.result = self._orphan_results.pop(call.id)
            entry.tool_calls.append(call)
            self._calls[call.id] = (call, entry)
        if live in self._tail:
            self._tail.remove(live)
        self._committed[index] = entry
        if live is None:
            self._notify(ADDED, entry.key)
        elif before == self.position(entry.key):
            self._notify(UPDATED, entry.key)
        else:  # it moved from the live part into its place among the committed messages
            self._notify(REMOVED, entry.key)
            self._notify(ADDED, entry.key)

    def _commit_tool(self, message: dict[str, Any]) -> None:
        call_id = message.get("tool_call_id", "")
        self.set_tool_result(call_id, [dict(p) for p in message.get("content") or []])

    def set_tool_result(self, call_id: str, content: list[dict[str, Any]], duration: float | None = None) -> None:
        found = self._calls.get(call_id)
        if found is None:
            self._orphan_results[call_id] = content
            return
        call, owner = found
        call.result, call.running = content, False
        if duration is not None:
            call.duration = duration
        self._notify(UPDATED, owner.key)

    def set_tool_running(self, call_id: str) -> None:
        found = self._calls.get(call_id)
        if found is not None and found[0].result is None:
            found[0].running = True
            self._notify(UPDATED, found[1].key)

    # --- the message that is being streamed -----------------------------------------------------------

    def _live_assistant(self, generation_id: str | None) -> AssistantEntry | None:
        return next((e for e in self._tail if isinstance(e, AssistantEntry) and e.generation_id == generation_id), None)

    def _live_entry(self, generation_id: str) -> AssistantEntry:
        entry = self._live_assistant(generation_id)
        if entry is None:
            entry = AssistantEntry(self._new_key("a"), generation_id)
            self._tail.append(entry)
            self._notify(ADDED, entry.key)
        return entry

    def live_text(self, generation_id: str, text: str) -> None:
        entry = self._live_entry(generation_id)
        entry.text += text
        self._notify(UPDATED, entry.key)

    def live_reasoning(self, generation_id: str, text: str) -> None:
        entry = self._live_entry(generation_id)
        entry.reasoning += text
        self._notify(UPDATED, entry.key)

    def live_tool_call(self, generation_id: str, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        entry = self._live_entry(generation_id)
        if call_id in self._calls:  # a replayed event
            return
        call = ToolCallView(call_id, name, dict(arguments))
        if call_id in self._orphan_results:
            call.result = self._orphan_results.pop(call_id)
        entry.tool_calls.append(call)
        self._calls[call_id] = (call, entry)
        self._notify(UPDATED, entry.key)

    # --- messages that the user sent ---------------------------------------------------------------------

    def add_pending_user(self, content: Sequence[dict[str, Any]], attachments: Sequence[Attachment] = ()) -> str:
        entry = UserEntry(self._new_key("u"), [dict(p) for p in content], None, SENDING, attachments=list(attachments))
        self._tail.append(entry)
        self._notify(ADDED, entry.key)
        return entry.key

    def set_uploaded(self, key: str, hashes: Sequence[str]) -> None:
        """The pictures of a pending message are on the backend: its content now names them (so that it can be matched
        with its committed copy)."""
        entry = self._pending(key)
        if entry is not None:
            entry.content = [p for p in entry.content if p.get("type") != "image"] + [{"type": "image", "hash": h} for h in hashes]
            self._notify(UPDATED, key)

    def mark_sent(self, key: str, index: int | None, queued: bool) -> None:
        """The backend accepted the message: committed at ``index``, or queued (``index`` is None)."""
        entry = self._pending(key)
        if entry is None:
            return  # it was matched with its committed copy already
        if index is None:
            entry.state, entry.error = QUEUED if queued else SENDING, None
            self._notify(UPDATED, key)
            return
        if index in self._seen:  # the committed copy arrived first (history sync)
            self._remove(entry)
            return
        before = self.position(key)
        self._tail.remove(entry)
        entry.index, entry.state, entry.error = index, COMMITTED, None
        self._committed[index] = entry
        self._seen.add(index)
        if before == self.position(key):
            self._notify(UPDATED, key)
        else:
            self._notify(REMOVED, key)
            self._notify(ADDED, key)

    def mark_failed(self, key: str, message: str, unknown: bool) -> None:
        entry = self._pending(key)
        if entry is not None:
            entry.state, entry.error = (UNKNOWN if unknown else FAILED), message
            self._notify(UPDATED, key)

    def resolve_unknown(self, key: str, message: str) -> None:
        """The backend was asked and does not have the message: it was not sent. (If it had it, the committed copy
        would have replaced the pending one already.)"""
        entry = self._pending(key)
        if entry is not None and entry.state == UNKNOWN:
            entry.state, entry.error = FAILED, message
            self._notify(UPDATED, key)

    def discard(self, key: str) -> None:
        """Remove a message that was not sent (a failed one)."""
        entry = self._pending(key)
        if entry is not None:
            self._remove(entry)

    def _pending(self, key: str) -> UserEntry | None:
        return next((e for e in self._tail if isinstance(e, UserEntry) and e.key == key), None)

    def _remove(self, entry: Entry) -> None:
        if entry in self._tail:
            self._tail.remove(entry)
        elif entry.index is not None and self._committed.get(entry.index) is entry:
            del self._committed[entry.index]
        else:
            return
        if isinstance(entry, AssistantEntry):
            for call in entry.tool_calls:
                self._calls.pop(call.id, None)
        self._notify(REMOVED, entry.key)
