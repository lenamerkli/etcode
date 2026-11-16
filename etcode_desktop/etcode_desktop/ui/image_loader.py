"""Pictures in the conversation: fetched from the backend once, kept on disk, shown as thumbnails.

* The backend stores pictures by the hash of their content, so a downloaded picture never changes. It is kept in a
  cache directory (private, pruned when it gets big) and in memory (the thumbnails that were used last).
* Downloading and decoding happen on a background thread. The GUI thread only turns the result into a pixmap.
* A picture that cannot be fetched is not asked for again for a short while, so that a broken one does not cause a
  request every time its message is redrawn.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QImage, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QScrollArea, QVBoxLayout, QWidget

from ..core.api import BackendClient
from ..core.errors import EtcodeError
from .dispatch import dispatcher
from .formatting import image_label

log = logging.getLogger(__name__)

THUMBNAIL_SIZE = QSize(240, 180)
MAX_CACHE_BYTES = 200 * 1024 * 1024
MEMORY_THUMBNAILS = 96
RETRY_AFTER_FAILURE_SECONDS = 20.0
_HASH = re.compile(r"^[0-9a-f]{64}$")

Callback = Callable[[QPixmap | None, str | None], None]  # (pixmap, None) or (None, why not)


class ImageLoader(QObject):
    def __init__(self, cache_dir: Path, get_client: Callable[[], BackendClient | None], *,
                 max_cache_bytes: int = MAX_CACHE_BYTES, parent: QObject | None = None):
        super().__init__(parent)
        self._dir = Path(cache_dir)
        self._get_client = get_client
        self._max_bytes = max_cache_bytes
        self._dispatch = dispatcher()
        self._thumbnails: OrderedDict[str, QPixmap] = OrderedDict()
        self._waiting: dict[tuple[str, str], list[Callback]] = {}
        self._failed: dict[tuple[str, str], tuple[float, str]] = {}
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.prune()

    # --- the cache on disk ----------------------------------------------------------------------------------

    def path_of(self, image_hash: str) -> Path:
        return self._dir / image_hash

    def prune(self) -> int:
        """Delete the least recently used files when the cache is over its limit. Returns how many were deleted."""
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p) for p in self._dir.iterdir() if p.is_file() and _HASH.fullmatch(p.name)]
        except OSError:
            return 0
        total = sum(size for _, size, _ in files)
        deleted = 0
        if total > self._max_bytes:
            for _, size, path in sorted(files):
                try:
                    path.unlink()
                except OSError:
                    continue
                total -= size
                deleted += 1
                if total <= self._max_bytes * 0.8:  # not just under the limit: leave some room
                    break
        return deleted

    def _store(self, image_hash: str, data: bytes) -> None:
        target = self.path_of(image_hash)
        if target.exists():
            return
        try:
            fd, temporary = tempfile.mkstemp(dir=self._dir, prefix=".part-")
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except OSError as exc:
            log.warning("could not cache the picture %s: %s", image_hash[:8], exc)

    def prime(self, image_hash: str, data: bytes) -> None:
        """A picture that this app uploaded itself: keep it, so that it never has to be downloaded."""
        if _HASH.fullmatch(image_hash):
            self._store(image_hash, data)

    # --- asking for a picture ----------------------------------------------------------------------------------

    def thumbnail(self, image_hash: str, callback: Callback) -> None:
        """A thumbnail, as soon as it is there (at once, if it is in memory). GUI thread only."""
        self._request(image_hash, "thumbnail", callback)

    def full(self, image_hash: str, callback: Callback) -> None:
        self._request(image_hash, "full", callback)

    def _request(self, image_hash: str, kind: str, callback: Callback) -> None:
        if not _HASH.fullmatch(image_hash):
            callback(None, "not a picture id")
            return
        key = (image_hash, kind)
        if kind == "thumbnail" and image_hash in self._thumbnails:
            self._thumbnails.move_to_end(image_hash)
            callback(self._thumbnails[image_hash], None)
            return
        failure = self._failed.get(key)
        if failure is not None and time.monotonic() - failure[0] < RETRY_AFTER_FAILURE_SECONDS:
            callback(None, failure[1])
            return
        if key in self._waiting:
            self._waiting[key].append(callback)  # the same picture is on its way already
            return
        self._waiting[key] = [callback]
        threading.Thread(target=self._work, args=(image_hash, kind), daemon=True, name=f"image-{image_hash[:6]}").start()

    def _bytes(self, image_hash: str) -> bytes:
        path = self.path_of(image_hash)
        try:
            data = path.read_bytes()
            os.utime(path)  # used just now: the last to be pruned
            return data
        except FileNotFoundError:
            pass
        client = self._get_client()
        if client is None:
            raise EtcodeError("there is no connection to the backend")
        data = client.download_image(image_hash).data
        self._store(image_hash, data)
        return data

    def _work(self, image_hash: str, kind: str) -> None:
        """Background thread: get the bytes, decode them (a QImage may be made on any thread, a QPixmap may not)."""
        image: QImage | None = None
        error: str | None = None
        try:
            data = self._bytes(image_hash)
            image = QImage.fromData(data)
            if image.isNull():
                image, error = None, "the picture cannot be shown"
            elif kind == "thumbnail" and (image.width() > THUMBNAIL_SIZE.width() or image.height() > THUMBNAIL_SIZE.height()):
                image = image.scaled(THUMBNAIL_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        except EtcodeError as exc:
            error = str(exc)
        except Exception as exc:  # never let a picture take a thread down silently
            log.exception("loading the picture %s failed", image_hash[:8])
            error = f"{type(exc).__name__}: {exc}"
        self._dispatch.post(self._done, image_hash, kind, image, error)

    def _done(self, image_hash: str, kind: str, image: QImage | None, error: str | None) -> None:
        key = (image_hash, kind)
        callbacks = self._waiting.pop(key, [])
        pixmap = QPixmap.fromImage(image) if image is not None else None
        if pixmap is not None:
            self._failed.pop(key, None)
            if kind == "thumbnail":
                self._thumbnails[image_hash] = pixmap
                while len(self._thumbnails) > MEMORY_THUMBNAILS:
                    self._thumbnails.popitem(last=False)
        else:
            self._failed[key] = (time.monotonic(), error or "the picture cannot be loaded")
        for callback in callbacks:
            try:
                callback(pixmap, None if pixmap is not None else error)
            except RuntimeError:
                pass  # the widget that asked has been deleted meanwhile

    def forget_failures(self) -> None:
        """Ask again for pictures that failed (after the connection came back, say)."""
        self._failed.clear()


def pixmap_from_bytes(data: bytes, limit: QSize | None = THUMBNAIL_SIZE) -> QPixmap | None:
    """For pictures that are at hand as bytes (the ones the user just attached). GUI thread."""
    image = QImage.fromData(data)
    if image.isNull():
        return None
    if limit is not None and (image.width() > limit.width() or image.height() > limit.height()):
        image = image.scaled(limit, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return QPixmap.fromImage(image)


class ImageViewerDialog(QDialog):
    """A picture at full size (scaled down to fit the screen if it is bigger)."""

    def __init__(self, loader: ImageLoader | None, image_hash: str | None, data: bytes | None, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._label = QLabel("Loading…")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidget(self._label)
        scroll.setWidgetResizable(True)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry().size() if screen is not None else QSize(1024, 768)
        self._limit = QSize(int(available.width() * 0.8), int(available.height() * 0.8))
        self.resize(min(self._limit.width(), 900), min(self._limit.height(), 700))
        if data is not None:
            self._show(pixmap_from_bytes(data, self._limit), None)
        elif loader is not None and image_hash:
            loader.full(image_hash, self._show)
        else:
            self._show(None, "no picture")

    def _show(self, pixmap: QPixmap | None, error: str | None) -> None:
        if pixmap is None:
            self._label.setText(f"The picture cannot be shown ({error})")
            return
        if pixmap.width() > self._limit.width() or pixmap.height() > self._limit.height():
            pixmap = pixmap.scaled(self._limit, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._label.setPixmap(pixmap)

    def shown_pixmap(self) -> QPixmap | None:
        pixmap = self._label.pixmap()
        return None if pixmap is None or pixmap.isNull() else pixmap


def open_viewer(parent: QWidget | None, loader: ImageLoader | None, image_hash: str | None, data: bytes | None, title: str) -> None:
    """Show a picture in a window of its own. (A function, so that tests can replace it.)"""
    dialog = ImageViewerDialog(loader, image_hash, data, title, parent)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.show()


class ThumbnailLabel(QLabel):
    """A small picture in a message. A click shows it large."""

    activated = Signal()

    def __init__(self, loader: ImageLoader | None, parent: QWidget | None = None):
        super().__init__(parent)
        self._loader = loader
        self._hash: str | None = None
        self._data: bytes | None = None
        self._name = ""
        self.setObjectName("thumbnail")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("QLabel { color: #6b7280; }")
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    @property
    def key(self) -> str:
        return self._hash or f"local:{self._name}:{len(self._data or b'')}"

    def show_hash(self, image_hash: str, data: bytes | None = None, name: str = "") -> None:
        """Show a picture of the backend. If its bytes are at hand (the user attached it), they are used instead of a download."""
        self._hash, self._data, self._name = image_hash, data, name
        self.setText(image_label(image_hash))
        self.setToolTip("Click to see it large")
        if data is not None:
            self._set_pixmap(pixmap_from_bytes(data), None)
        elif self._loader is not None:
            self._loader.thumbnail(image_hash, self._set_pixmap)

    def show_bytes(self, data: bytes, name: str) -> None:
        """A picture that is not on the backend yet."""
        self._hash, self._data, self._name = None, data, name
        self.setText(f"[{name}]")
        self.setToolTip(f"{name}. Click to see it large")
        self._set_pixmap(pixmap_from_bytes(data), None)

    def _set_pixmap(self, pixmap: QPixmap | None, error: str | None) -> None:
        if pixmap is not None:
            self.setPixmap(pixmap)
            self.setText("")
            self.setFixedSize(pixmap.size())
        else:
            self.setText(f"[picture not available: {error}]" if error else self.text())
            self.setToolTip(error or self.toolTip())

    def has_picture(self) -> bool:
        pixmap = self.pixmap()
        return pixmap is not None and not pixmap.isNull()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit()
            open_viewer(self.window(), self._loader, self._hash, self._data, self._name or "Picture")
        super().mousePressEvent(event)

