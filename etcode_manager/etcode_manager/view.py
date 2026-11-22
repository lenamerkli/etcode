"""VNC screen widget: displays the VM framebuffer and forwards input.

A ``QWidget`` that embeds an :class:`RFBClient`, painting the framebuffer
scaled to fit and translating mouse / keyboard / wheel events into RFB
input messages.  Everything runs over QEMU's built-in VNC server, so the
guest needs no additional software.

The RFB reader thread never touches widgets directly; frames and status
changes are marshalled to the GUI thread through queued signals.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal, QSize, QPointF
from PySide6.QtGui import (
    QGuiApplication, QImage, QPainter, QColor, QFocusEvent, QKeyEvent, QMouseEvent, QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from .keysyms import key_event_to_keysym
from .rfb import RFBClient, RFBError
from .scancodes import native_scan_to_qnum
from .scaling import ScalingFilter, fitted_rect, resample_frame


class VncView(QWidget):
    """Remote screen widget with mouse and keyboard passthrough."""

    # Cross-thread notifications (queued connections -> GUI thread).
    connected = Signal(str)        # desktop name
    disconnected = Signal(str)     # reason
    error = Signal(str)
    _frame_ready = Signal(object)  # QImage, private

    def __init__(self, parent=None, scaling_filter: ScalingFilter | str = ScalingFilter.AREA):
        super().__init__(parent)
        self._client: RFBClient | None = None
        self._pressed_keys: dict[tuple[bool, int], tuple[int, int | None]] = {}
        self._image: QImage | None = None
        self._button_mask = 0
        self._pointer = (0, 0)     # last known framebuffer coords
        self._status = "Not connected"
        self._scaling_filter = ScalingFilter(scaling_filter)
        self._scaled_image: QImage | None = None
        self._scaled_key: tuple | None = None

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Let the guest handle dead keys and compose, not the host input method.
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setMouseTracking(True)  # Forward pointer motion even without a pressed button.
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
        self._release_keys()
        client, self._client = self._client, None
        if client is not None:
            client.close()
        self._image = None
        self._invalidate_scaled_image()
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
        self._image = image if not image.isNull() else None
        self._invalidate_scaled_image()
        self._status = f"{image.width()}×{image.height()}"
        self.update()

    # -- scaling ------------------------------------------------------------

    @property
    def scaling_filter(self) -> ScalingFilter:
        return self._scaling_filter

    def set_scaling_filter(self, filter: ScalingFilter | str) -> None:
        filter = ScalingFilter(filter)
        if filter != self._scaling_filter:
            self._scaling_filter = filter
            self._invalidate_scaled_image()
            self.update()

    def _invalidate_scaled_image(self) -> None:
        self._scaled_image = None
        self._scaled_key = None

    def _display_image(self, size: QSize, dpr: float) -> QImage:
        """Cache one resampled frame, not a resize on every expose/repaint.

        Include DPR as well as physical size: a move between monitors can
        change image metadata even when the required pixel dimensions match.
        """
        image = self._image
        assert image is not None
        key = (image.cacheKey(), size.width(), size.height(), dpr, self._scaling_filter)
        if self._scaled_image is None or key != self._scaled_key:
            self._scaled_image = resample_frame(image, size, self._scaling_filter)
            self._scaled_image.setDevicePixelRatio(dpr)
            self._scaled_key = key
        return self._scaled_image

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))
        if self._image is None:
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status)
            return

        dpr = self.devicePixelRatioF()
        rect = fitted_rect(self._image.size(), self.size(), dpr)
        if rect.isEmpty():
            return
        image = self._display_image(rect.size(), dpr)
        # The cached image already has its final physical dimensions.  Drawing
        # at a device-aligned point with matching DPR is a 1:1 pixel copy, not
        # a second bilinear resize to a floating-point target rectangle.
        painter.drawImage(QPointF(rect.x() / dpr, rect.y() / dpr), image)

    def _to_screen(self, pos) -> tuple[int, int]:
        """Map through the exact, device-rounded rectangle used for painting.

        Calculate it here too, so input arriving just after a resize (before
        the next paint) never uses stale offsets or scale factors.
        """
        if self._image is None:
            return 0, 0
        dpr = self.devicePixelRatioF()
        rect = fitted_rect(self._image.size(), self.size(), dpr)
        if rect.isEmpty():
            return 0, 0
        x = int((pos.x() * dpr - rect.x()) * self._image.width() / rect.width())
        y = int((pos.y() * dpr - rect.y()) * self._image.height() / rect.height())
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

    def event(self, event: QEvent) -> bool:
        if (event.type() == QEvent.Type.ShortcutOverride
                and self._client is not None and self._client.ready):
            event.accept()  # don't let manager shortcuts consume guest key presses
            return True
        return super().event(event)

    def focusNextPrevChild(self, next: bool) -> bool:
        # QWidget otherwise consumes Tab/Backtab for its own focus traversal.
        if self._client is not None and self._client.ready:
            return False
        return super().focusNextPrevChild(next)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self._release_keys()
        super().focusOutEvent(event)

    def _release_keys(self) -> None:
        client = self._client
        if client is not None:
            for keysym, keycode in reversed(self._pressed_keys.values()):
                self._send_key(client, keysym, keycode, False)
        self._pressed_keys.clear()

    @staticmethod
    def _send_key(client: RFBClient, keysym: int, keycode: int | None, down: bool) -> None:
        if keycode is None:
            client.send_key(keysym, down)
        else:
            client.send_extended_key(keysym, keycode, down)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        self._key_event(event, down=True)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        self._key_event(event, down=False)

    def _key_event(self, event: QKeyEvent, down: bool) -> None:
        client = self._client
        if client is None or not client.ready:
            event.ignore()
            return

        # Qt's logical key and text can both change when a modifier is released.
        # Pair each release with the same packet format and code as the press,
        # even if negotiation just finished.
        scan = event.nativeScanCode()
        identity = (bool(scan), scan or int(event.key()))
        if event.isAutoRepeat() and not down:
            event.accept()  # Qt may generate a release/press pair for autorepeat
            return
        if down:
            pressed = self._pressed_keys.get(identity)
            if pressed is None and not event.isAutoRepeat():
                platform = QGuiApplication.platformName()
                keycode = native_scan_to_qnum(platform, scan) if client.supports_extended_keys else None
                keysym = None
                if keycode is not None and (platform == "xcb" or platform.startswith("wayland")):
                    # Qt passes the XKB keysym here, including keypad and dead keys.
                    keysym = event.nativeVirtualKey()
                keysym = keysym or key_event_to_keysym(event)
                if keycode is not None or keysym is not None:
                    pressed = self._pressed_keys[identity] = (keysym or 0, keycode)
        else:
            pressed = self._pressed_keys.pop(identity, None)

        if pressed is None:
            event.ignore()
        else:
            self._send_key(client, *pressed, down)
            event.accept()