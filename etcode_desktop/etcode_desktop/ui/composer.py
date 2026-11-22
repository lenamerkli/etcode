"""The box where the user types a message: text, pictures, and Send and Stop.

Pictures come from the attach button, from the clipboard (paste) and from files that are dropped on the box. They
are checked (format and size) when they are added, shown as small chips that can be removed, and go with the message.
"""
from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent, QPaintEvent, QPainter
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from ..core.attachments import MAX_ATTACHMENTS, Attachment, AttachmentError, attachment_from_bytes, load_attachment
from .image_loader import pixmap_from_bytes

MIN_LINES = 2
MAX_LINES = 8
CHIP_THUMBNAIL = QSize(40, 40)
PROBLEM_SECONDS = 6
MESSAGE_PLACEHOLDER = "Message… (Ctrl+Enter sends, Enter adds a line)"
IMAGE_FILTER = "Pictures (*.png *.jpg *.jpeg *.webp *.gif);;All files (*)"


def choose_image_files(parent: QWidget | None) -> list[str]:
    """Ask for picture files. (A function, so that tests can replace it.)"""
    paths, _ = QFileDialog.getOpenFileNames(parent, "Attach pictures", "", IMAGE_FILTER)
    return list(paths)


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data.data())


class _Edit(QPlainTextEdit):
    """Ctrl+Enter sends. Enter makes a new line. Pictures can be pasted or dropped."""

    submit = Signal()
    pictures_given = Signal(list)  # list of (name, bytes) or file paths: see Composer

    def __init__(self) -> None:
        super().__init__()
        self.setTabChangesFocus(True)
        # Use the requested height when it fits, but let the layout shrink the box to keep the transcript visible.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._user_height: int | None = None  # set by dragging the handle above the box
        self.document().documentLayout().documentSizeChanged.connect(self.updateGeometry)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and modifiers == Qt.KeyboardModifier.ControlModifier:
            self.submit.emit()
            return
        super().keyPressEvent(event)

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        return source.hasImage() or self._files_in(source) != [] or super().canInsertFromMimeData(source)

    @staticmethod
    def _files_in(source: QMimeData) -> list[str]:
        if not source.hasUrls():
            return []
        urls = source.urls()
        return [u.toLocalFile() for u in urls] if urls and all(u.isLocalFile() for u in urls) else []

    def insertFromMimeData(self, source: QMimeData) -> None:
        if source.hasImage():
            image = source.imageData()
            if isinstance(image, QImage) and not image.isNull():
                self.pictures_given.emit([("pasted.png", png_bytes(image))])
                return
        files = self._files_in(source)
        if files:
            self.pictures_given.emit(files)
            return
        super().insertFromMimeData(source)

    def _lines_height(self, lines: int) -> int:
        margins = int(self.document().documentMargin() * 2) + 2 * self.frameWidth() + 2
        return lines * self.fontMetrics().lineSpacing() + margins

    def set_user_height(self, height: int | None) -> None:
        """Choose a height, or return to automatic sizing with None."""
        if height is not None:
            height = max(height, self._lines_height(MIN_LINES))
        if height != self._user_height:
            self._user_height = height
            self.updateGeometry()

    def minimumSizeHint(self) -> QSize:
        return QSize(super().minimumSizeHint().width(), self._lines_height(MIN_LINES))

    def sizeHint(self) -> QSize:
        if self._user_height is not None:
            return QSize(200, self._user_height)
        lines = min(max(int(self.document().size().height()), MIN_LINES), MAX_LINES)
        return QSize(200, self._lines_height(lines))


class _ResizeHandle(QFrame):
    """A grip above the text box: drag it up or down to resize. Double-click to reset."""

    def __init__(self, edit: _Edit):
        super().__init__()
        self._edit = edit
        self._start_y: float | None = None
        self._start_height = 0
        self.setObjectName("composerResizeHandle")
        self.setAccessibleName("Resize message box")
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setFixedHeight(10)
        self.setToolTip("Drag up or down to resize the message box. Double-click for automatic sizing.")

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        # A visible grip in the center of the full-width drag target.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().windowText())
        painter.setOpacity(0.6)
        width, height = 48, 4
        painter.drawRoundedRect((self.width() - width) // 2, (self.height() - height) // 2, width, height, 2, 2)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_y = event.globalPosition().y()
            self._start_height = self._edit.height()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._start_y is not None and event.buttons() & Qt.MouseButton.LeftButton:
            # Use global coordinates because the handle itself moves. Dragging up makes the box taller.
            self._edit.set_user_height(round(self._start_height + self._start_y - event.globalPosition().y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._start_y is not None:
            self._start_y = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_y = None
            self._edit.set_user_height(None)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class _Chip(QFrame):
    removed = Signal(object)  # the Attachment

    def __init__(self, attachment: Attachment):
        super().__init__()
        self.attachment = attachment
        self.setObjectName("attachmentChip")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        thumb = QLabel()
        pixmap = pixmap_from_bytes(attachment.data, CHIP_THUMBNAIL)
        if pixmap is not None:
            thumb.setPixmap(pixmap)
        name = QLabel(attachment.name if len(attachment.name) <= 24 else attachment.name[:21] + "…")
        name.setToolTip(f"{attachment.name} ({attachment.size // 1024} KiB)")
        remove = QToolButton()
        remove.setObjectName("removeAttachment")
        remove.setText("✕")
        remove.setToolTip("Do not send this picture")
        remove.clicked.connect(lambda: self.removed.emit(self.attachment))
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.addWidget(thumb)
        row.addWidget(name)
        row.addWidget(remove)


class Composer(QWidget):
    send_requested = Signal(str)  # the text (it may be empty if there are pictures). The pictures: attachments()
    stop_requested = Signal()
    attachments_changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._busy = False
        self._images_allowed = True
        self._images_reason = ""
        self._attachments: list[Attachment] = []
        self._chips: list[_Chip] = []

        self._edit = _Edit()
        self._edit.setObjectName("composerEdit")
        self._edit.setPlaceholderText(MESSAGE_PLACEHOLDER)
        self._edit.submit.connect(self._submit)
        self._edit.pictures_given.connect(self._on_pictures_given)
        self._edit.textChanged.connect(self._update_send_enabled)
        self._problem = QLabel()
        self._problem.setObjectName("composerProblem")
        self._problem.setStyleSheet("color: #8a1f1f;")
        self._problem.setWordWrap(True)
        self._problem.setVisible(False)
        self._problem_timer = QTimer(self)
        self._problem_timer.setSingleShot(True)
        self._problem_timer.timeout.connect(lambda: self._problem.setVisible(False))
        self._strip = QWidget()
        self._strip.setObjectName("attachmentStrip")
        self._strip_layout = QHBoxLayout(self._strip)
        self._strip_layout.setContentsMargins(0, 0, 0, 0)
        self._strip_layout.addStretch(1)
        self._strip.setVisible(False)

        self._attach = QPushButton("📎 Picture")
        self._attach.setObjectName("attachButton")
        self._attach.setToolTip("Attach a picture (or paste one, or drop a file here)")
        self._attach.clicked.connect(self._choose)
        self._send = QPushButton("Send")
        self._send.setObjectName("sendButton")
        self._send.setDefault(True)
        self._send.clicked.connect(self._submit)
        self._stop = QPushButton("Stop")
        self._stop.setObjectName("stopButton")
        self._stop.setEnabled(False)
        self._stop.clicked.connect(self.stop_requested)
        self._sending_allowed = True
        buttons = QHBoxLayout()
        buttons.addWidget(self._attach)
        buttons.addStretch(1)
        buttons.addWidget(self._stop)
        buttons.addWidget(self._send)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.addWidget(self._problem)
        layout.addWidget(self._strip)
        self._resize_handle = _ResizeHandle(self._edit)
        layout.addWidget(self._resize_handle)
        layout.addWidget(self._edit)
        layout.addLayout(buttons)

    # --- sending ---------------------------------------------------------------------------------------------------

    def _submit(self) -> None:
        text = self._edit.toPlainText().strip()
        if (text or self._attachments) and self._sending_allowed:
            self.send_requested.emit(text)

    def _update_send_enabled(self) -> None:
        pass  # Send stays enabled: an empty message is simply not sent

    # --- state -------------------------------------------------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        """While the backend works, a new message is queued (it is sent when the work is done), and Stop is on."""
        self._busy = busy
        self._send.setText("Queue" if busy else "Send")
        self._stop.setEnabled(busy)

    def set_sending_allowed(self, allowed: bool, reason: str = "") -> None:
        self._sending_allowed = allowed
        self._edit.setEnabled(allowed)
        self._send.setEnabled(allowed)
        self._attach.setEnabled(allowed and self._images_allowed)
        self._edit.setPlaceholderText(reason if (reason and not allowed) else MESSAGE_PLACEHOLDER)

    def set_images_allowed(self, allowed: bool, reason: str = "") -> None:
        """Whether the model of the session can look at pictures. If not, the attach button is off."""
        self._images_allowed, self._images_reason = allowed, reason
        self._attach.setEnabled(allowed and self._sending_allowed)
        self._attach.setToolTip("Attach a picture (or paste one, or drop a file here)" if allowed else reason)

    @property
    def images_allowed(self) -> bool:
        return self._images_allowed

    # --- text ------------------------------------------------------------------------------------------------------------

    def text(self) -> str:
        return self._edit.toPlainText()

    def set_text(self, text: str) -> None:
        self._edit.setPlainText(text)

    def clear(self) -> None:
        """Empty the box: the text and the pictures."""
        self._edit.clear()
        self.clear_attachments()

    def focus_input(self) -> None:
        self._edit.setFocus()

    # --- pictures ------------------------------------------------------------------------------------------------------------

    def attachments(self) -> list[Attachment]:
        return list(self._attachments)

    def _choose(self) -> None:
        self._on_pictures_given(choose_image_files(self))

    def _on_pictures_given(self, items: Sequence[object]) -> None:
        """Pictures from the file dialog, a paste or a drop: file paths, or (name, bytes) pairs."""
        for item in items:
            try:
                if isinstance(item, tuple):
                    self.add_attachment(attachment_from_bytes(item[1], item[0]))
                else:
                    self.add_attachment(load_attachment(str(item)))
            except AttachmentError as exc:
                self.show_problem(str(exc))

    def add_attachment(self, attachment: Attachment) -> bool:
        if not self._images_allowed:
            self.show_problem(self._images_reason or "This model cannot look at pictures.")
            return False
        if len(self._attachments) >= MAX_ATTACHMENTS:
            self.show_problem(f"At most {MAX_ATTACHMENTS} pictures can be sent with one message.")
            return False
        self._attachments.append(attachment)
        chip = _Chip(attachment)
        chip.removed.connect(self.remove_attachment)
        self._strip_layout.insertWidget(len(self._chips), chip)
        self._chips.append(chip)
        self._strip.setVisible(True)
        self.attachments_changed.emit()
        return True

    def remove_attachment(self, attachment: Attachment) -> None:
        for chip in list(self._chips):
            if chip.attachment is attachment:
                self._chips.remove(chip)
                self._strip_layout.removeWidget(chip)
                chip.setParent(None)
                chip.deleteLater()
        if attachment in self._attachments:
            self._attachments.remove(attachment)
        self._strip.setVisible(bool(self._attachments))
        self.attachments_changed.emit()

    def clear_attachments(self) -> None:
        for attachment in list(self._attachments):
            self.remove_attachment(attachment)

    def show_problem(self, text: str) -> None:
        self._problem.setText(text)
        self._problem.setVisible(True)
        self._problem_timer.start(PROBLEM_SECONDS * 1000)

    # for tests
    @property
    def edit(self) -> QPlainTextEdit:
        return self._edit

    @property
    def resize_handle(self) -> QFrame:
        return self._resize_handle

    @property
    def send_button(self) -> QPushButton:
        return self._send

    @property
    def stop_button(self) -> QPushButton:
        return self._stop

    @property
    def attach_button(self) -> QPushButton:
        return self._attach

    @property
    def problem_text(self) -> str:
        return self._problem.text() if not self._problem.isHidden() else ""

    @property
    def chips(self) -> list[_Chip]:
        return list(self._chips)
