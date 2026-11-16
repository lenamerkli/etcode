"""Images that the user attaches to a message.

An image is checked before anything is uploaded: the format (by its first bytes, not by its name) and the size.
The backend takes PNG, JPEG, WebP and GIF up to 20 MiB.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # the backend's upload limit
MAX_ATTACHMENTS = 10  # per message (the window's limit: the backend has none)


class AttachmentError(Exception):
    """The image cannot be attached. The message says why, for the user."""


@dataclass(frozen=True)
class Attachment:
    name: str
    data: bytes
    mime: str

    @property
    def size(self) -> int:
        return len(self.data)


def sniff_image(data: bytes) -> str | None:
    """The MIME type from the first bytes, for the formats the backend takes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def attachment_from_bytes(data: bytes, name: str = "image") -> Attachment:
    if not data:
        raise AttachmentError(f"{name} is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise AttachmentError(f"{name} is too large ({len(data) // (1024 * 1024)} MiB). The limit is {MAX_IMAGE_BYTES // (1024 * 1024)} MiB")
    mime = sniff_image(data)
    if mime is None:
        raise AttachmentError(f"{name} is not a PNG, JPEG, WebP or GIF image")
    return Attachment(name, data, mime)


def load_attachment(path: str | os.PathLike[str]) -> Attachment:
    """Read an image file. The size is checked before the file is read."""
    target = Path(path)
    try:
        if target.is_dir():
            raise AttachmentError(f"{target.name} is a folder, not an image")
        size = target.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise AttachmentError(f"{target.name} is too large ({size // (1024 * 1024)} MiB). The limit is {MAX_IMAGE_BYTES // (1024 * 1024)} MiB")
        data = target.read_bytes()
    except FileNotFoundError:
        raise AttachmentError(f"{target.name} does not exist") from None
    except PermissionError:
        raise AttachmentError(f"{target.name} cannot be read (permission denied)") from None
    except OSError as exc:
        raise AttachmentError(f"{target.name} cannot be read: {exc.strerror or exc}") from None
    return attachment_from_bytes(data, target.name)
