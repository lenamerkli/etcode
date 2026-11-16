"""The ``view_image`` tool: upload an image to the backend and hand its hash to the model.

The backend passes the image on to the model in a message of its own, so the result only has to contain
the hash (and a line of text for people who read the transcript).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..api import BackendClient
from ..errors import BackendError, Unreachable
from ..models import image_part, text_part
from .files import FileToolError, resolve_path

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # the backend's upload limit


def sniff(data: bytes) -> str | None:
    """The MIME type from the first bytes, for the formats that view_image takes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def view_image(path: str, home: Path, client: BackendClient) -> list[dict[str, Any]]:
    target = resolve_path(path, home)
    try:
        if target.is_dir():
            raise FileToolError(f"{path} is a directory, not an image")
        size = target.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise FileToolError(f"{path} is too large ({size} bytes). The limit is {MAX_IMAGE_BYTES} bytes")
        data = target.read_bytes()
    except FileNotFoundError:
        raise FileToolError(f"file not found: {path}") from None
    except PermissionError:
        raise FileToolError(f"permission denied: {path}") from None
    except OSError as exc:
        raise FileToolError(f"cannot read {path}: {exc.strerror or exc}") from None
    mime = sniff(data)
    if mime is None:
        raise FileToolError(f"{path} is not a PNG, JPEG or WebP image")
    try:
        uploaded = client.upload_image(data, target.name)
    except BackendError as exc:
        raise FileToolError(f"the backend did not accept the image: {exc.message}") from None
    except Unreachable as exc:
        raise FileToolError(f"the image could not be uploaded: {exc}") from None
    return [image_part(uploaded.hash), text_part(f"Image {path} ({mime}, {len(data)} bytes)")]
