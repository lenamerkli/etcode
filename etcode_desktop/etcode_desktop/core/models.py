"""Typed views of the backend's JSON, and builders for the messages the client sends.

Messages themselves stay plain dictionaries in the backend's wire format
(``{"role": "user", "content": [...]}``). The controller only needs a few fields, and
keeping the dictionaries avoids dropping fields that a newer backend adds.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import ProtocolError

# --- stream event kinds (see the backend README, "SSE events") --------------------

REASONING_DELTA = "reasoning_delta"
TEXT_DELTA = "text_delta"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
USAGE = "usage"
MESSAGE_COMPLETE = "message_complete"
ERROR = "error"
CANCELLED = "cancelled"
DONE = "done"

# --- session status values ---------------------------------------------------------

STATUS_IDLE = "idle"
STATUS_GENERATING = "generating"
STATUS_AWAITING_TOOLS = "awaiting_tools"
STATUS_FAILED = "failed"

IMAGE_HASH_LENGTH = 64


def _require(data: Mapping[str, Any], key: str, what: str) -> Any:
    try:
        return data[key]
    except (KeyError, TypeError):
        raise ProtocolError(f"{what} has no '{key}' field") from None


def _as_dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{what} is not a JSON object")
    return value


def parse_time(value: Any) -> float:
    """Seconds since the epoch from a number or an ISO 8601 string. Unknown input gives 0.0."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return 0.0


# --- sessions ---------------------------------------------------------------------

@dataclass(frozen=True)
class Cost:
    currency: str = "USD"
    total: float = 0.0
    total_exact: str = "0"  # exact decimal string: use this for bookkeeping, not the float
    unpriced_generations: int = 0  # > 0 means `total` is only a lower bound

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Cost:
        if not data:
            return cls()
        total = data.get("total", 0.0)
        return cls(
            currency=str(data.get("currency", "USD")),
            total=float(total) if isinstance(total, (int, float)) else 0.0,
            total_exact=str(data.get("total_exact", total)),
            unpriced_generations=int(data.get("unpriced_generations", 0) or 0),
        )


@dataclass(frozen=True)
class PendingToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    resolved: bool  # a result was accepted, but the session is still waiting for the other calls

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PendingToolCall:
        data = _as_dict(data, "pending tool call")
        arguments = data.get("arguments", {})
        return cls(
            id=str(_require(data, "id", "pending tool call")),
            name=str(_require(data, "name", "pending tool call")),
            arguments=arguments if isinstance(arguments, dict) else {},
            resolved=bool(data.get("resolved", False)),
        )


@dataclass(frozen=True)
class SessionMeta:
    id: str
    model: str
    status: str
    websearch: bool = False
    options: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)
    message_count: int = 0
    queued_count: int = 0
    active_generation_id: str | None = None
    cost: Cost = field(default_factory=Cost)
    created_at: float = 0.0
    updated_at: float = 0.0
    title: str | None = None  # only reported by `GET /sessions`
    # None means "this backend does not report it" (an older backend), which is not the same as "no calls".
    pending_tool_calls: tuple[PendingToolCall, ...] | None = None

    @classmethod
    def from_dict(cls, data: Any) -> SessionMeta:
        data = _as_dict(data, "session")
        pending = data.get("pending_tool_calls")
        if pending is not None and not isinstance(pending, list):
            raise ProtocolError("session 'pending_tool_calls' is not a list")
        options = data.get("options")
        title = data.get("title")
        return cls(
            id=str(_require(data, "id", "session")),
            model=str(_require(data, "model", "session")),
            status=str(_require(data, "status", "session")),
            websearch=bool(data.get("websearch", False)),
            options=options if isinstance(options, dict) else {},
            message_count=int(data.get("message_count", 0) or 0),
            queued_count=int(data.get("queued_count", 0) or 0),
            active_generation_id=data.get("active_generation_id") or None,
            cost=Cost.from_dict(data.get("cost")),
            created_at=parse_time(data.get("created_at")),
            updated_at=parse_time(data.get("updated_at")),
            title=title if isinstance(title, str) else None,
            pending_tool_calls=None if pending is None else tuple(PendingToolCall.from_dict(p) for p in pending),
        )

    def unresolved_client_calls(self) -> tuple[PendingToolCall, ...]:
        """The calls this client has to execute. ``websearch`` is run by the backend."""
        return tuple(c for c in (self.pending_tool_calls or ()) if not c.resolved and c.name != "websearch")


@dataclass(frozen=True)
class SessionList:
    sessions: tuple[SessionMeta, ...]
    total: int

    @classmethod
    def from_json(cls, data: Any) -> SessionList:
        # `{"sessions": [...], "total": n}`; a bare list is accepted as well.
        if isinstance(data, list):
            items, total = data, len(data)
        else:
            data = _as_dict(data, "session list")
            items = _require(data, "sessions", "session list")
            total = data.get("total", len(items) if isinstance(items, list) else 0)
        if not isinstance(items, list):
            raise ProtocolError("'sessions' is not a list")
        return cls(tuple(SessionMeta.from_dict(i) for i in items), int(total))


# --- catalog, history, submit, media ---------------------------------------------------

@dataclass(frozen=True)
class ModelInfo:
    name: str
    tech_name: str
    context_length: int
    supports_images: bool
    provider: str
    available: bool = True  # False when the backend has no API key for the provider
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)

    _KNOWN = ("name", "tech_name", "context_length", "supports_images", "provider", "available")

    @classmethod
    def from_dict(cls, data: Any) -> ModelInfo:
        data = _as_dict(data, "model")
        return cls(
            name=str(_require(data, "name", "model")),
            tech_name=str(_require(data, "tech_name", "model")),
            context_length=int(data.get("context_length", 0) or 0),
            supports_images=bool(data.get("supports_images", False)),
            provider=str(data.get("provider", "")),
            available=bool(data.get("available", True)),
            extra={k: v for k, v in data.items() if k not in cls._KNOWN},
        )


@dataclass(frozen=True)
class HistoryEntry:
    index: int
    message: dict[str, Any]

    @classmethod
    def from_dict(cls, data: Any) -> HistoryEntry:
        data = _as_dict(data, "history entry")
        index = _require(data, "index", "history entry")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ProtocolError("history entry index is not an integer")
        return cls(index, _as_dict(_require(data, "message", "history entry"), "history message"))


@dataclass(frozen=True)
class SubmitResult:
    # one entry per submitted message: {"index"| "queue_id", "queued"} for user messages,
    # {"tool_call_id", "accepted"| "duplicate"} for tool results
    accepted: tuple[dict[str, Any], ...]
    generation_id: str | None  # set if this submission started a generation
    session: SessionMeta

    @property
    def queued(self) -> bool:
        return any(a.get("queued") for a in self.accepted)

    @classmethod
    def from_dict(cls, data: Any) -> SubmitResult:
        data = _as_dict(data, "submit response")
        accepted = data.get("accepted", [])
        if not isinstance(accepted, list):
            raise ProtocolError("'accepted' is not a list")
        return cls(
            accepted=tuple(a for a in accepted if isinstance(a, dict)),
            generation_id=data.get("generation_id") or None,
            session=SessionMeta.from_dict(_require(data, "session", "submit response")),
        )


@dataclass(frozen=True)
class UploadedImage:
    hash: str
    media_type: str
    mime_type: str

    @classmethod
    def from_dict(cls, data: Any) -> UploadedImage:
        data = _as_dict(data, "upload response")
        return cls(
            hash=str(_require(data, "hash", "upload response")),
            media_type=str(data.get("media_type", "image")),
            mime_type=str(data.get("mime_type", "")),
        )


@dataclass(frozen=True)
class DownloadedImage:
    data: bytes
    mime_type: str


# --- stream events ---------------------------------------------------------------------

@dataclass(frozen=True)
class StreamEvent:
    """One server-sent event. ``data`` is the decoded JSON payload."""

    kind: str
    data: dict[str, Any]
    event_id: str | None  # "<generation_id>:<sequence>", sent back as Last-Event-ID
    generation_id: str | None
    sequence: int | None

    @property
    def is_done(self) -> bool:
        return self.kind == DONE

    @property
    def next_generation_id(self) -> str | None:
        """For a ``done`` event: the generation to follow next (after websearch or a queued message)."""
        return self.data.get("next_generation_id") if self.kind == DONE else None


# --- message builders ------------------------------------------------------------------

def text_part(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def image_part(image_hash: str) -> dict[str, str]:
    return {"type": "image", "hash": image_hash}


def user_message(text: str = "", image_hashes: Iterable[str] = ()) -> dict[str, Any]:
    content: list[dict[str, str]] = []
    hashes = list(image_hashes)
    if text or not hashes:
        content.append(text_part(text))
    content.extend(image_part(h) for h in hashes)
    return {"role": "user", "content": content}


def tool_message(tool_call_id: str, content: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A tool result. ``content`` must not be empty: the backend rejects an empty result."""
    if not content:
        raise ValueError("a tool result needs at least one content part")
    return {"role": "tool", "tool_call_id": tool_call_id, "content": [dict(c) for c in content]}
