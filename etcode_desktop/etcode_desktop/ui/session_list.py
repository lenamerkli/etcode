"""The sidebar: the sessions of the current backend and token."""
from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from ..core.store import SessionRecord
from .formatting import display_title, status_color, subtitle, tooltip

ID_ROLE = Qt.ItemDataRole.UserRole


def status_icon(color: str, size: int = 12) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pixmap)


class SessionListWidget(QWidget):
    new_session_requested = Signal()
    attach_requested = Signal()
    session_selected = Signal(object)  # the session id, or None when nothing is selected
    rename_requested = Signal(str, str)  # id, the new title (empty: back to the automatic title)
    archive_requested = Signal(str, bool)  # id, archived
    show_archived_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumWidth(220)
        self._icons: dict[str, QIcon] = {}
        self._records: dict[str, SessionRecord] = {}

        self._new_button = QPushButton("+ New session")
        self._new_button.setObjectName("newSessionButton")
        self._new_button.clicked.connect(self.new_session_requested)

        more = QToolButton()
        more.setObjectName("moreButton")
        more.setText("⋯")
        more.setToolTip("More")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        self._attach_action = menu.addAction("Add a session by its id…")
        self._attach_action.triggered.connect(self.attach_requested)
        more.setMenu(menu)

        self._empty = QLabel("No sessions yet.")
        self._empty.setObjectName("emptyHint")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setEnabled(False)

        self._list = QListWidget()
        self._list.setObjectName("sessionList")
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.currentItemChanged.connect(self._on_current_changed)
        self._list.setUniformItemSizes(False)

        self._show_archived = QCheckBox("Show removed sessions")
        self._show_archived.setObjectName("showArchived")
        self._show_archived.toggled.connect(self.show_archived_changed)

        top = QHBoxLayout()
        top.addWidget(self._new_button, 1)
        top.addWidget(more)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addLayout(top)
        layout.addWidget(self._empty)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._show_archived)

    # --- content -----------------------------------------------------------------------------

    def set_records(self, records: Sequence[SessionRecord], now: float | None = None) -> None:
        """Show these sessions, in this order. The selection and scroll position stay where they were."""
        previous = self.selected_id()
        scroll = self._list.verticalScrollBar().value()
        self._records = {r.id: r for r in records}
        self._list.blockSignals(True)
        self._list.clear()
        for record in records:
            self._list.addItem(self._make_item(record, now))
        keep = self._row_of(previous) if previous else -1
        self._list.setCurrentRow(keep)
        self._list.blockSignals(False)
        self._list.verticalScrollBar().setValue(scroll)
        self._empty.setVisible(not records)
        self._list.setVisible(bool(records))
        if previous and keep < 0:
            self.session_selected.emit(None)  # the selected session is gone from the list

    def _make_item(self, record: SessionRecord, now: float | None) -> QListWidgetItem:
        item = QListWidgetItem(f"{display_title(record)}\n{subtitle(record, now)}")
        item.setData(ID_ROLE, record.id)
        item.setToolTip(tooltip(record, now))
        color = status_color(record.status)
        icon = self._icons.get(color)
        if icon is None:
            icon = self._icons[color] = status_icon(color)
        item.setIcon(icon)
        if record.is_missing or record.archived:
            item.setForeground(QBrush(QColor("#8a8f98")))
            font = QFont(item.font())
            font.setItalic(True)
            item.setFont(font)
        return item

    def _row_of(self, session_id: str | None) -> int:
        for row in range(self._list.count()):
            if self._list.item(row).data(ID_ROLE) == session_id:
                return row
        return -1

    def selected_id(self) -> str | None:
        item = self._list.currentItem()
        return item.data(ID_ROLE) if item is not None else None

    def select(self, session_id: str) -> bool:
        row = self._row_of(session_id)
        if row < 0:
            return False
        self._list.setCurrentRow(row)
        return True

    def ids(self) -> list[str]:
        return [self._list.item(row).data(ID_ROLE) for row in range(self._list.count())]

    def focus_list(self) -> None:
        """Keyboard focus to the list, with a session selected (the first, if none is)."""
        self._list.setFocus()
        if self._list.currentRow() < 0 and self._list.count():
            self._list.setCurrentRow(0)

    def set_new_enabled(self, enabled: bool) -> None:
        self._new_button.setEnabled(enabled)

    def set_show_archived(self, shown: bool) -> None:
        self._show_archived.setChecked(shown)

    # --- interaction ---------------------------------------------------------------------------------

    def _on_current_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        self.session_selected.emit(current.data(ID_ROLE) if current is not None else None)

    def _on_context_menu(self, position: QPoint) -> None:
        item = self._list.itemAt(position)
        if item is None:
            return
        self._menu_for(item.data(ID_ROLE)).exec(self._list.viewport().mapToGlobal(position))

    def _menu_for(self, session_id: str) -> QMenu:
        """The context menu of a session. Its actions are named, so that tests can find them."""
        record = self._records[session_id]
        menu = QMenu(self)
        rename = menu.addAction("Rename…")
        rename.setObjectName("renameAction")
        rename.triggered.connect(lambda: self.rename(session_id))
        copy = menu.addAction("Copy session id")
        copy.setObjectName("copyIdAction")
        copy.triggered.connect(lambda: self.copy_id(session_id))
        menu.addSeparator()
        if record.archived:
            archive: QAction = menu.addAction("Show in the list again")
        else:
            archive = menu.addAction("Remove from list")
            archive.setToolTip("The session stays on the backend. It can't be deleted there.")
        archive.setObjectName("archiveAction")
        archive.triggered.connect(lambda: self.archive_requested.emit(session_id, not record.archived))
        return menu

    def rename(self, session_id: str) -> None:
        record = self._records[session_id]
        title, accepted = QInputDialog.getText(self, "Rename session", "Title (empty: use the automatic title):",
                                               text=record.title if record.title_custom else "")
        if accepted:
            self.rename_requested.emit(session_id, title)

    def copy_id(self, session_id: str) -> None:
        QApplication.clipboard().setText(session_id)
