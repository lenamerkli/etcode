"""Export a snapshot of a chat's committed messages to the VM filesystem."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..api import BackendClient
from ..errors import BackendError, EtcodeError, Unreachable
from ..models import HistoryEntry
from .cancel import CancelToken
from .files import FileToolError, resolve_path
from .schemas import ToolArgumentError

_IMAGE_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def export_messages(directory: str, *, client: BackendClient, session_id: str, home: Path, cancel: CancelToken) -> str:
    if not directory.strip():
        raise ToolArgumentError("directory must not be blank")
    if "\0" in directory:
        raise ToolArgumentError("directory must not contain a NUL character")
    destination = resolve_path(directory, home)
    try:
        _check_cancel(cancel)
        if os.path.lexists(destination):  # also matches dangling symlinks
            raise _destination_exists(destination)
        try:
            history = client.messages(session_id)
        except EtcodeError as exc:
            raise FileToolError(f"cannot fetch chat history: {_backend_problem(exc)}") from None
        _check_cancel(cancel)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".save-messages-") as temporary:
            staging = Path(temporary)
            seen: set[int] = set()
            count = 0
            for entry in history:
                _check_cancel(cancel)
                if entry.index < 0 or entry.index in seen:
                    raise FileToolError("the backend returned invalid message indices")
                seen.add(entry.index)
                count += _write_message(staging, entry, client, cancel)
            _publish(staging, destination, cancel)
    except OSError as exc:
        raise FileToolError(f"cannot save messages to {destination}: {exc.strerror or exc}") from None
    messages = "message" if len(history) == 1 else "messages"
    files = "file" if count == 1 else "files"
    return f"Saved {len(history)} {messages} ({count} {files}) to {destination}"


def _backend_problem(exc: EtcodeError) -> str:
    # Don't send arbitrary error bodies (which can contain private data) back as tool output.
    if isinstance(exc, Unreachable):
        return "the backend could not be reached"
    if isinstance(exc, BackendError):
        return f"the backend returned HTTP {exc.status}"
    return "unexpected response from the backend"


def _check_cancel(cancel: CancelToken) -> None:
    if cancel.cancelled:
        raise FileToolError("saving messages was cancelled")


def _destination_exists(destination: Path) -> FileToolError:
    return FileToolError(f"destination already exists: {destination}; choose a new directory")


def _publish(staging: Path, destination: Path, cancel: CancelToken) -> None:
    _check_cancel(cancel)
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError:
        raise _destination_exists(destination) from None
    try:
        _check_cancel(cancel)
        # On POSIX, rename replaces the empty directory we just reserved. Concurrent
        # exports to this path fail at mkdir instead of replacing an existing export.
        staging.rename(destination)
    except BaseException:
        try:
            destination.rmdir()  # remove only the empty reservation, never a directory tree
        except OSError:
            pass
        raise


def _write_message(staging: Path, entry: HistoryEntry, client: BackendClient, cancel: CancelToken) -> int:
    message = entry.message
    role = message.get("role")
    prefix = f"{entry.index:04d}"
    if role == "assistant":
        label = f"{prefix}_assistant"
        text = message.get("text")
        _write_private(staging / f"{label}.txt", b"" if text is None else _text_bytes(text, label), cancel)
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            raise FileToolError(f"invalid tool calls in {label}")
        if calls:
            _write_private(staging / f"{label}_calls.json", _calls_bytes(calls, label), cancel)
            return 2
        return 1
    if role not in ("system", "user", "tool"):
        raise FileToolError(f"unsupported role in message {prefix}")
    parts = message.get("content")
    if not isinstance(parts, list):
        raise FileToolError(f"invalid content in message {prefix}")
    for index, part in enumerate(parts):
        _check_cancel(cancel)
        label = f"{prefix}-{index}_{role}"
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            data, extension = _text_bytes(part, label), ".txt"
        elif kind == "image":
            data, extension = _download_image(client, part.get("hash"), label)
        else:
            raise FileToolError(f"unsupported content type in {label}")
        _write_private(staging / f"{label}{extension}", data, cancel)
    return len(parts)


def _utf8(text: str, label: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise FileToolError(f"cannot encode {label} as UTF-8") from None


def _text_bytes(part: object, label: str) -> bytes:
    if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
        raise FileToolError(f"invalid text in {label}")
    return _utf8(part["text"], label)


def _calls_bytes(calls: list[Any], label: str) -> bytes:
    exported = []
    for call in calls:
        if (not isinstance(call, dict) or not isinstance(call.get("id"), str)
                or not isinstance(call.get("name"), str) or not isinstance(call.get("arguments"), dict)):
            raise FileToolError(f"invalid tool call in {label}")
        exported.append({key: call[key] for key in ("id", "name", "arguments")})
    try:
        text = json.dumps(exported, ensure_ascii=False, allow_nan=False, indent=2)
    except (TypeError, ValueError):
        raise FileToolError(f"cannot encode tool calls in {label} as JSON") from None
    return _utf8(text + "\n", label)


def _download_image(client: BackendClient, image_hash: object, label: str) -> tuple[bytes, str]:
    if not isinstance(image_hash, str):
        raise FileToolError(f"invalid image reference in {label}")
    try:
        image = client.download_image(image_hash)
    except ValueError:
        raise FileToolError(f"invalid image reference in {label}") from None
    except EtcodeError as exc:
        raise FileToolError(f"cannot download image for {label}: {_backend_problem(exc)}") from None
    extension = _IMAGE_EXTENSIONS.get(image.mime_type.partition(";")[0].strip().lower())
    if extension is None or not image.data:
        raise FileToolError(f"unsupported or empty image in {label}")
    return image.data, extension


def _write_private(path: Path, data: bytes, cancel: CancelToken) -> None:
    _check_cancel(cancel)
    with open(path, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
        output.write(data)
