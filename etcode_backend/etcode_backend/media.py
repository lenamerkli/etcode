"""Content-addressed images, scoped to the uploader's token owner."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import filetype

from .config import Settings
from .db import now
from .errors import APIError

IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def save_image(con: sqlite3.Connection, settings: Settings, owner: str, content: bytes) -> dict:
    if not content or len(content) > settings.max_upload_bytes:
        raise APIError("invalid_image", "Image is empty or exceeds the upload size limit", 413)
    kind = filetype.guess(content)
    if kind is None or kind.mime not in IMAGE_MIMES:
        raise APIError("unsupported_media", "Supported formats: PNG, JPEG, WebP, GIF", 415)
    hash_ = hashlib.sha256(content).hexdigest()
    path = settings.image_dir / hash_
    settings.image_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        # Atomic creation: concurrent identical uploads are safe.
        import os
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
    con.execute("INSERT OR IGNORE INTO images VALUES (?,?,?,?,?)", (hash_, owner, kind.mime, len(content), now()))
    return {"hash": hash_, "media_type": "image", "mime_type": kind.mime}


def image_record(con: sqlite3.Connection, owner: str, hash_: str):
    row = con.execute("SELECT mime,size FROM images WHERE hash=? AND owner=?", (hash_, owner)).fetchone()
    if row is None:
        raise APIError("image_not_found", "Image not found", 404)
    return row


def validate_content(con: sqlite3.Connection, owner: str, content: list) -> None:
    for item in content:
        if item.type == "image":
            image_record(con, owner, item.hash)


def image_path(settings: Settings, hash_: str) -> Path:
    return settings.image_dir / hash_
