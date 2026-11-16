"""The conversation: one block per message, in a scroll area.

Blocks are only changed when their entry changes, and changes of a streaming message are applied at
most every 50 ms, so a fast stream does not make the window crawl. The view stays at the bottom while
new text arrives, unless the user scrolled up. Then a "jump to latest" button appears.
"""
from __future__ import annotations

import json

from PySide6.QtCore import QSize, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from ..core import transcript as t
from .formatting import parts_text, tool_summary, truncate
from .image_loader import ImageLoader, ThumbnailLabel
from .markdown_view import AutoHeightBrowser

FLUSH_INTERVAL_MS = 50
AT_BOTTOM_TOLERANCE = 6  # pixels
RESULT_PREVIEW_CHARS = 4000

MUTED = "color: #6b7280;"
ERROR_STYLE = "background: #fdecec; color: #8a1f1f; border: 1px solid #f1b5b5; border-radius: 4px; padding: 6px;"
NOTE_STYLE = "background: #f3f4f6; color: #374151; border-radius: 4px; padding: 4px 6px;"


def _label(text: str = "", style: str = "", object_name: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if style:
        label.setStyleSheet(style)
    if object_name:
        label.setObjectName(object_name)
    return label


class ThumbnailRow(QWidget):
    """Pictures in a message, side by side. Only redrawn when the pictures change."""

    def __init__(self, loader: ImageLoader | None, parent: QWidget | None = None):
        super().__init__(parent)
        self._loader = loader
        self._keys: list[str] = []
        self._labels: list[ThumbnailLabel] = []
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        self.setVisible(False)

    def set_pictures(self, pictures: list[tuple[str, bytes | None, str]]) -> None:
        """Each picture: (hash or "", its bytes if they are at hand, a name). Without a hash, the bytes are shown."""
        keys = [f"{h}|{len(d or b'')}|{n}" for h, d, n in pictures]
        if keys == self._keys:
            return
        self._keys = keys
        for label in self._labels:
            self._layout.removeWidget(label)
            label.setParent(None)
            label.deleteLater()
        self._labels = []
        for position, (image_hash, data, name) in enumerate(pictures):
            label = ThumbnailLabel(self._loader)
            if image_hash:
                label.show_hash(image_hash, data, name)
            elif data is not None:
                label.show_bytes(data, name or "picture")
            self._layout.insertWidget(position, label)
            self._labels.append(label)
        self.setVisible(bool(pictures))

    @property
    def labels(self) -> list[ThumbnailLabel]:
        return list(self._labels)


class ElidedToolButton(QToolButton):
    """A button whose text is cut with '…' when it does not fit, instead of making its parent wider."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._full = ""
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self._elide()

    def full_text(self) -> str:
        return self._full

    def _elide(self) -> None:
        available = max(self.width() - 16, 0)
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, available) if available else self._full
        if shown != self.text():
            self.setText(shown)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())


class ToolCardWidget(QFrame):
    """One tool call: what it does, and (once there is one) its result. Click the header to see the details."""

    def __init__(self, parent: QWidget | None = None, loader: ImageLoader | None = None):
        super().__init__(parent)
        self.setObjectName("toolCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._expanded = False
        self._call: t.ToolCallView | None = None
        self._header = ElidedToolButton()
        self._header.setObjectName("toolHeader")
        self._header.setCheckable(True)
        self._header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._header.setStyleSheet("QToolButton { text-align: left; border: none; padding: 3px; }")
        self._header.toggled.connect(self._toggle)
        self._pictures = ThumbnailRow(loader)
        self._pictures.setObjectName("toolPictures")
        self._details = AutoHeightBrowser()
        self._details.setObjectName("toolDetails")
        self._details.setStyleSheet("font-family: monospace;")
        self._details.setVisible(False)
        self._more = QPushButton("Show all")
        self._more.setObjectName("toolShowAll")
        self._more.setVisible(False)
        self._more.clicked.connect(self._show_all)
        self._show_everything = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)
        layout.addWidget(self._header)
        layout.addWidget(self._pictures)
        layout.addWidget(self._details)
        layout.addWidget(self._more, 0, Qt.AlignmentFlag.AlignLeft)

    def update_from(self, call: t.ToolCallView) -> None:
        self._call = call
        status = self._status_text(call)
        summary = tool_summary(call.name, call.arguments)
        arrow = "▾" if self._expanded else "▸"
        self._header.set_full_text(f"{arrow} {call.name}  {summary}   ·  {status}".rstrip())
        self._header.setToolTip(f"{call.name} {summary} ({status})".strip())
        self._pictures.set_pictures([(p["hash"], None, "") for p in call.result or [] if p.get("type") == "image" and "hash" in p])
        self._refresh_details()

    @staticmethod
    def _status_text(call: t.ToolCallView) -> str:
        if call.result is None:
            return "running…" if call.running else "waiting"
        failed = parts_text(call.result).startswith("Error:")
        text = "failed" if failed else "done"
        return f"{text} in {call.duration:.1f} s" if call.duration is not None else text

    def _toggle(self, expanded: bool) -> None:
        self._expanded = expanded
        if self._call is not None:
            self.update_from(self._call)

    def _show_all(self) -> None:
        self._show_everything = True
        self._refresh_details()

    def _refresh_details(self) -> None:
        self._details.setVisible(self._expanded)
        self._more.setVisible(False)
        if not self._expanded or self._call is None:
            return
        arguments = json.dumps(self._call.arguments, indent=2, ensure_ascii=False)
        text = f"Arguments\n{arguments}"
        if self._call.result is not None:
            result = parts_text(self._call.result)
            shown = result if self._show_everything else truncate(result, RESULT_PREVIEW_CHARS)
            text += f"\n\nResult\n{shown}"
            self._more.setVisible(len(shown) < len(result))
        self._details.set_plain(text)

    # for tests
    @property
    def header_text(self) -> str:
        return self._header.full_text()

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._header.setChecked(expanded)

    def details_text(self) -> str:
        return self._details.shown_text()


class UserBlock(QFrame):
    retry_requested = Signal(str)  # the key of the entry: send this message again
    discard_requested = Signal(str)  # the key of the entry: remove this message

    def __init__(self, parent: QWidget | None = None, loader: ImageLoader | None = None):
        super().__init__(parent)
        self._key = ""
        self.setObjectName("userBlock")
        self.setStyleSheet("#userBlock { background: #eef4ff; border-radius: 6px; }")
        self._who = _label("You", MUTED + " font-size: 11px;")
        self._body = AutoHeightBrowser()
        self._body.setObjectName("userText")
        self._pictures = ThumbnailRow(loader)
        self._pictures.setObjectName("userPictures")
        self._state = _label("", "", "userState")
        self._retry = QPushButton("Send again")
        self._retry.setObjectName("retryMessageButton")
        self._retry.clicked.connect(lambda: self.retry_requested.emit(self._key))
        self._discard = QPushButton("Remove")
        self._discard.setObjectName("discardMessageButton")
        self._discard.clicked.connect(lambda: self.discard_requested.emit(self._key))
        self._actions = QWidget()
        actions = QHBoxLayout(self._actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(self._retry)
        actions.addWidget(self._discard)
        actions.addStretch(1)
        self._actions.setVisible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)
        layout.addWidget(self._who)
        layout.addWidget(self._body)
        layout.addWidget(self._pictures)
        layout.addWidget(self._state)
        layout.addWidget(self._actions)

    def update_from(self, entry: t.UserEntry) -> None:
        self._key = entry.key
        self._actions.setVisible(entry.state == t.FAILED)  # only a message that surely did not arrive can be sent again
        self._body.set_plain(entry.text)
        self._body.setVisible(bool(entry.text))
        hashes, local = entry.image_hashes, entry.attachments
        if hashes and len(hashes) == len(local):  # uploaded: the app has the bytes, so nothing needs to be downloaded
            pictures = [(h, a.data, a.name) for h, a in zip(hashes, local)]
        elif hashes:
            pictures = [(h, None, "") for h in hashes]
        else:
            pictures = [("", a.data, a.name) for a in local]
        self._pictures.set_pictures(pictures)
        text, style = {
            t.SENDING: ("sending…", MUTED),
            t.QUEUED: ("queued: it is sent when the current work is done", MUTED),
            t.FAILED: (f"not sent: {entry.error}", ERROR_STYLE),
            t.UNKNOWN: (f"not confirmed: {entry.error}. It may still have arrived. If it did, it appears here.", ERROR_STYLE),
        }.get(entry.state, ("", ""))
        self._state.setText(text)
        self._state.setStyleSheet(style)
        self._state.setVisible(bool(text))

    @property
    def state_text(self) -> str:
        return self._state.text()

    def body_text(self) -> str:
        return self._body.shown_text()

    @property
    def thumbnails(self) -> list[ThumbnailLabel]:
        return self._pictures.labels


class AssistantBlock(QFrame):
    def __init__(self, parent: QWidget | None = None, loader: ImageLoader | None = None):
        super().__init__(parent)
        self._loader = loader
        self.setObjectName("assistantBlock")
        self._who = _label("Assistant", MUTED + " font-size: 11px;")
        self._thinking = QToolButton()
        self._thinking.setObjectName("thinkingToggle")
        self._thinking.setCheckable(True)
        self._thinking.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._thinking.setStyleSheet("QToolButton { border: none; text-align: left; " + MUTED + " }")
        self._thinking.toggled.connect(self._toggle_thinking)
        self._reasoning = AutoHeightBrowser()
        self._reasoning.setObjectName("reasoningText")
        self._reasoning.setStyleSheet(MUTED)
        self._reasoning.setVisible(False)
        self._body = AutoHeightBrowser()
        self._body.setObjectName("assistantText")
        self._tools = QVBoxLayout()
        self._tools.setSpacing(3)
        self._cards: dict[str, ToolCardWidget] = {}
        self._note = _label("", "", "assistantNote")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(3)
        layout.addWidget(self._who)
        layout.addWidget(self._thinking, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._reasoning)
        layout.addWidget(self._body)
        layout.addLayout(self._tools)
        layout.addWidget(self._note)
        self._reasoning_text = ""
        self._live = False
        self._has_text = False  # the answer itself has started (so the thinking is over)

    def _toggle_thinking(self, shown: bool) -> None:
        self._reasoning.setVisible(shown and bool(self._reasoning_text))
        self._thinking.setText(self._thinking_label(shown))

    def _thinking_label(self, shown: bool) -> str:
        return ("▾ " if shown else "▸ ") + ("Thinking…" if self._live and not self._has_text else "Thinking")

    def update_from(self, entry: t.AssistantEntry) -> None:
        self._live = entry.is_live
        self._has_text = bool(entry.text.strip())
        self._reasoning_text = entry.reasoning
        has_reasoning = bool(entry.reasoning.strip())
        self._thinking.setVisible(has_reasoning)
        self._thinking.setText(self._thinking_label(self._thinking.isChecked()))
        self._reasoning.setVisible(has_reasoning and self._thinking.isChecked())
        if has_reasoning:
            self._reasoning.set_plain(entry.reasoning)
        self._body.set_markdown(entry.text)
        self._body.setVisible(bool(entry.text.strip()))
        self._sync_cards(entry.tool_calls)
        text, style = "", ""
        if entry.status == "error":
            error = entry.error or {}
            detail = error.get("message") or "The model could not finish this answer."
            code = f" ({error['code']})" if error.get("code") else ""
            retry = " You can send another message to continue." if error.get("retryable") else ""
            text, style = f"Error: {detail}{code}.{retry}", ERROR_STYLE
        elif entry.status == "cancelled":
            text, style = "Stopped.", NOTE_STYLE
        elif entry.is_live and not entry.text and not entry.tool_calls and not has_reasoning:
            text, style = "…", MUTED
        self._note.setText(text)
        self._note.setStyleSheet(style)
        self._note.setVisible(bool(text))

    def _sync_cards(self, calls: list[t.ToolCallView]) -> None:
        for call in calls:
            card = self._cards.get(call.id)
            if card is None:
                card = self._cards[call.id] = ToolCardWidget(loader=self._loader)
                self._tools.addWidget(card)
            card.update_from(call)

    # for tests
    def card(self, call_id: str) -> ToolCardWidget | None:
        return self._cards.get(call_id)

    @property
    def cards(self) -> dict[str, ToolCardWidget]:
        return self._cards

    def body_text(self) -> str:
        return self._body.shown_text()

    def reasoning_shown_text(self) -> str:
        return self._reasoning.shown_text()

    @property
    def note_text(self) -> str:
        return self._note.text()

    @property
    def thinking_visible(self) -> bool:
        return not self._thinking.isHidden()

    def set_thinking_expanded(self, expanded: bool) -> None:
        self._thinking.setChecked(expanded)

    @property
    def thinking_text(self) -> str:
        return self._thinking.text()


class TranscriptView(QScrollArea):
    retry_requested = Signal(str)  # a message that was not sent: send it again (the key of its entry)
    discard_requested = Signal(str)  # ... or remove it

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("transcript")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)
        self.setWidget(self._container)

        self._model: t.TranscriptModel | None = None
        self._loader: ImageLoader | None = None
        self._blocks: dict[str, QWidget] = {}
        self._dirty: set[str] = set()
        self._stick_to_bottom = True
        self._pinning = False  # True while we move the scroll bar ourselves
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(FLUSH_INTERVAL_MS)
        self._timer.timeout.connect(self.flush)
        self._pin_timer = QTimer(self)  # owned by the view, so that it dies with it (QTimer.singleShot would not)
        self._pin_timer.setSingleShot(True)
        self._pin_timer.setInterval(0)
        self._pin_timer.timeout.connect(self._pin)

        self._jump = QPushButton("↓ Jump to latest", self)
        self._jump.setObjectName("jumpToLatest")
        self._jump.hide()
        self._jump.clicked.connect(self.scroll_to_bottom)
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._on_scrolled)
        bar.rangeChanged.connect(self._on_range_changed)

    def set_image_loader(self, loader: ImageLoader | None) -> None:
        """Where pictures come from. Blocks that exist already keep the loader they were made with."""
        self._loader = loader

    # --- the model -------------------------------------------------------------------------------------

    def set_model(self, model: t.TranscriptModel | None) -> None:
        if self._model is not None:
            self._model.unsubscribe(self._on_change)
        self._model = model
        self._stick_to_bottom = True
        self._rebuild()
        if model is not None:
            model.subscribe(self._on_change)

    def _on_change(self, change: t.Change) -> None:
        if change.kind == t.RESET:
            self._rebuild()
        elif change.kind == t.ADDED and change.key is not None:
            self._add(change.key)
        elif change.kind == t.REMOVED and change.key is not None:
            self._remove(change.key)
        elif change.kind == t.UPDATED and change.key is not None:
            self._dirty.add(change.key)
            if not self._timer.isActive():
                self._timer.start()

    def _rebuild(self) -> None:
        for key in list(self._blocks):
            self._remove(key)
        self._dirty.clear()
        if self._model is not None:
            for entry in self._model.entries():
                self._add(entry.key)
        self.scroll_to_bottom()

    def _add(self, key: str) -> None:
        assert self._model is not None
        entry = self._model.get(key)
        if entry is None or key in self._blocks:
            return
        block: QWidget
        if isinstance(entry, t.UserEntry):
            user_block = UserBlock(loader=self._loader)
            user_block.retry_requested.connect(self.retry_requested)
            user_block.discard_requested.connect(self.discard_requested)
            block = user_block
        else:
            block = AssistantBlock(loader=self._loader)
        block.update_from(entry)  # type: ignore[attr-defined]
        position = self._model.position(key)
        self._layout.insertWidget(min(position if position is not None else 0, len(self._blocks)), block)
        self._blocks[key] = block
        self._grew()

    def _remove(self, key: str) -> None:
        block = self._blocks.pop(key, None)
        self._dirty.discard(key)
        if block is not None:
            self._layout.removeWidget(block)
            block.setParent(None)
            block.deleteLater()

    def flush(self) -> None:
        """Apply the pending changes now (they are applied by a timer otherwise)."""
        if self._model is None:
            self._dirty.clear()
            return
        keys, self._dirty = self._dirty, set()
        for key in keys:
            block, entry = self._blocks.get(key), self._model.get(key)
            if block is not None and entry is not None:
                block.update_from(entry)  # type: ignore[attr-defined]
        if keys:
            self._grew()

    # --- scrolling ----------------------------------------------------------------------------------------

    def _at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - AT_BOTTOM_TOLERANCE

    def _on_scrolled(self, _value: int) -> None:
        if not self._pinning:
            self._stick_to_bottom = self._at_bottom()
        self._jump.setVisible(not self._stick_to_bottom and self._model is not None and bool(self._blocks))
        self._place_jump_button()

    def _on_range_changed(self, _low: int, _high: int) -> None:
        if self._stick_to_bottom:
            self._pin()
        self._jump.setVisible(not self._stick_to_bottom and bool(self._blocks))

    def _grew(self) -> None:
        if self._stick_to_bottom:
            self._pin_timer.start()  # after the layout has caught up with the new content

    def _pin(self) -> None:
        self._pinning = True
        try:
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum())
        finally:
            self._pinning = False

    def scroll_to_bottom(self) -> None:
        self._stick_to_bottom = True
        self._jump.hide()
        self._pin()
        self._pin_timer.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_jump_button()

    def _place_jump_button(self) -> None:
        self._jump.adjustSize()
        self._jump.move(self.width() - self._jump.width() - 20, self.height() - self._jump.height() - 12)

    # --- for tests ---------------------------------------------------------------------------------------------

    def block(self, key: str) -> QWidget | None:
        return self._blocks.get(key)

    def blocks_in_order(self) -> list[QWidget]:
        return [self._layout.itemAt(i).widget() for i in range(self._layout.count() - 1)]

    @property
    def jump_button(self) -> QPushButton:
        return self._jump

    @property
    def at_bottom(self) -> bool:
        return self._at_bottom()

