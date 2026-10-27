"""VNC screen widget: displays the VM framebuffer and forwards input.

A ``QWidget`` that embeds an :class:`RFBClient`, painting the framebuffer
scaled to fit and translating mouse / keyboard / wheel events into RFB
input messages.  Everything runs over QEMU's built-in VNC server, so the
guest needs no additional software.

The RFB reader thread never touches widgets directly; frames and status
changes are marshalled to the GUI thread through queued signals.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage, QPainter, QColor, QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from .keysyms import key_event_to_keysym
from .rfb import RFBClient, RFBError


class VncView(QWidget):
    """Remote screen widget with mouse and keyboard passthrough."""

    # Cross-thread notifications (queued connections -> GUI thread).
    connected = Signal(str)        # desktop name
    disconnected = Signal(str)     # reason
    error = Signal(str)
    _frame_ready = Signal(object)  # QImage, private

    def __init__(self, parent=None):
        super().__init__(parent)
        self._client: RFBClient | None = None
        self._image: QImage | None = None
        self._button_mask = 0
        self._pointer = (0, 0)     # last known framebuffer coords
        self._status = "Not connected"
        self._scale = 1.0
        self._offset = (0, 0)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(QSize(640, 400))
        self._frame_ready.connect(self._set_image)

    # -- connection ---------------------------------------------------------

    def connect_to(self, host: str, port: int, timeout: float = 10.0) -> None:
        self.disconnect_from()
        client = RFBClient(
            host,
            port,
            on_frame=self._on_frame,
            on_disconnect=self._on_disconnect,
        )
        try:
            client.connect(timeout)
        except RFBError as exc:
            self.error.emit(str(exc))
            return
        self._client = client
        self._status = f"Connecting to {host}:{port}…"
        self.update()

    def disconnect_from(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()
        self._image = None
        self._button_mask = 0
        self._status = "Not connected"
        self.update()

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    # -- RFB callbacks (reader thread -> GUI thread) ------------------------

    def _on_frame(self, rects, width: int, height: int, framebuffer: bytes) -> None:
        image = QImage(framebuffer, width, height, width * 4, QImage.Format.Format_RGB32).copy()
        self._frame_ready.emit(image)

    def _on_disconnect(self, reason: str) -> None:
        self.disconnected.emit(reason)

    def _set_image(self, image: QImage) -> None:
        self._image = image
        self._status = f"{image.width()}×{image.height()}"
        self.update()

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))
        if self._image is None:
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status)
            return

        image = self._image
        scale = min(self.width() / image.width(), self.height() / image.height())
        w, h = int(image.width() * scale), int(image.height() * scale)
        x = (self.width() - w) // 2
        y = (self.height() - h) // 2
        self._scale, self._offset = scale, (x, y)
        painter.drawImage(QRect(x, y, w, h), image)

    def _to_screen(self, pos) -> tuple[int, int]:
        """Map a widget position to framebuffer coordinates."""
        if self._image is None or self._scale <= 0:
            return 0, 0
        x = int((pos.x() - self._offset[0]) / self._scale)
        y = int((pos.y() - self._offset[1]) / self._scale)
        return (
            max(0, min(x, self._image.width() - 1)),
            max(0, min(y, self._image.height() - 1)),
        )

    # -- input capture ------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.setFocus()
        self._button_mask |= self._mask_for(event.button())
        self._pointer_event(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._button_mask &= ~self._mask_for(event.button())
        self._pointer_event(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._pointer_event(event)

    def _pointer_event(self, event: QMouseEvent) -> None:
        if self._client is None or self._image is None:
            return
        x, y = self._to_screen(event.position())
        self._pointer = (x, y)
        self._client.send_pointer(self._button_mask, x, y)

    @staticmethod
    def _mask_for(button: Qt.MouseButton) -> int:
        return {
            Qt.MouseButton.LeftButton: 0x01,
            Qt.MouseButton.MiddleButton: 0x02,
            Qt.MouseButton.RightButton: 0x04,
        }.get(button, 0)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._client is None:
            return
        button = 0x08 if event.angleDelta().y() > 0 else 0x10
        self._client.send_pointer(button, *self._pointer)
        self._client.send_pointer(0, *self._pointer)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        self._key_event(event, down=True)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        self._key_event(event, down=False)

    def _key_event(self, event: QKeyEvent, down: bool) -> None:
        if self._client is None:
            return
        keysym = key_event_to_keysym(event)
        if keysym is not None:
            self._client.send_key(keysym, down)
            event.accept()
        else:
            event.ignore()