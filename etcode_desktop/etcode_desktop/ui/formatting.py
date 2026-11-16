"""Plain functions that turn store records into text and colours. No widgets in here."""
from __future__ import annotations

import time
from datetime import datetime

from ..core.store import STATUS_MISSING, SessionRecord

UNTITLED = "New session"

# status -> (colour of the dot, label)
_STATUS = {
    "idle": ("#8a8f98", "idle"),
    "generating": ("#2f7de1", "working"),
    "awaiting_tools": ("#e08a1e", "running tools"),
    "failed": ("#d64545", "failed"),
    STATUS_MISSING: ("#b9bcc2", "not on the backend"),
}
_UNKNOWN = ("#b9bcc2", "unknown")


def display_title(record: SessionRecord) -> str:
    return record.title.strip() or UNTITLED


def status_color(status: str | None) -> str:
    return _STATUS.get(status or "", _UNKNOWN)[0]


def status_label(status: str | None) -> str:
    return _STATUS.get(status or "", _UNKNOWN)[1]


def format_age(timestamp: float, now: float | None = None) -> str:
    """'just now', '5 min ago', '3 h ago', 'yesterday', '4 days ago', or a date."""
    now = time.time() if now is None else now
    seconds = max(now - timestamp, 0.0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def format_cost(cost_total: str | None) -> str:
    """'$0.0123' from the backend's exact decimal string. Unknown stays empty."""
    if not cost_total:
        return ""
    return "$" + cost_total


def subtitle(record: SessionRecord, now: float | None = None) -> str:
    parts = []
    if record.model:
        parts.append(record.model)
    if record.is_missing:
        parts.append("not on the backend")
    parts.append(format_age(record.updated_at, now))
    if record.archived:
        parts.append("removed")
    return " · ".join(parts)


def tooltip(record: SessionRecord, now: float | None = None) -> str:
    lines = [display_title(record), "", f"Status: {status_label(record.status)}"]
    if record.model:
        lines.append(f"Model: {record.model}")
    if record.cost_total:
        lines.append(f"Cost: {format_cost(record.cost_total)}")
    lines.append(f"Last activity: {format_age(record.updated_at, now)}")
    lines.append(f"Created: {datetime.fromtimestamp(record.created_at).strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Id: {record.id}")
    return "\n".join(lines)


# --- the conversation ------------------------------------------------------------------------------------------

SUMMARY_LENGTH = 90


def truncate(text: str, limit: int, marker: str = "…") -> str:
    if len(text) <= limit:
        return text
    if limit <= len(marker):  # no room for the marker
        return text[:max(limit, 0)]
    return text[:limit - len(marker)] + marker


def tool_summary(name: str, arguments: dict) -> str:
    """A short line that says what a tool call does: the command, the path, the query."""
    if "_invalid_arguments" in arguments:
        return "(arguments are not valid JSON)"
    key = {"bash": "command", "read_file": "path", "write_to_file": "path", "replace_in_file": "path",
           "view_image": "path", "websearch": "query"}.get(name)
    if key is not None and isinstance(arguments.get(key), str):
        first_line = arguments[key].strip().splitlines()[0] if arguments[key].strip() else ""
        return truncate(first_line, SUMMARY_LENGTH)
    for key, value in arguments.items():
        return truncate(f"{key}={value}", SUMMARY_LENGTH)
    return ""


def parts_text(parts: list[dict] | None) -> str:
    """The text of tool result content parts. An image is shown as '[image]'."""
    if not parts:
        return ""
    return "\n".join(p.get("text", "") if p.get("type") == "text" else "[image]" for p in parts)


def image_label(image_hash: str) -> str:
    return f"[image {image_hash[:8]}…]"
