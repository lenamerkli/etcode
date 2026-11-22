"""The main window: the session sidebar on the left, the conversation of the selected session on the right.

Everything that talks to the backend goes through :class:`~etcode_desktop.ui.bridge.BackendBridge` and the
conversations' controllers, so the window never waits for the network.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QInputDialog, QLabel, QMainWindow, QMessageBox, QPushButton, QSplitter, QStackedWidget, QToolButton, QWidget,
)

from .. import __version__
from ..config import KEY_TOKEN, SOURCE_ENV, SOURCE_GUI, SOURCE_PROVISIONED, ConfigStore, Settings
from ..core.options import SessionOptions
from ..core.store import SessionRecord, SessionStore
from ..core.tools.scheduler import ToolRunner
from ..logging_setup import register_secret
from . import bridge as states
from .bridge import BackendBridge
from .chat_pane import ChatPane
from .chat_session import ChatSession
from .connection_dialog import ConnectionDialog
from .image_loader import ImageLoader
from .new_session_dialog import NewSessionDialog
from .session_list import SessionListWidget

log = logging.getLogger(__name__)

WINDOW_TITLE = "etcode"
DEFAULT_SIZE = (1100, 750)
KV_GEOMETRY = "window_geometry"
KV_SPLITTER = "splitter_sizes"
KV_LAST_MODEL = "last_model"
KV_SHOW_ARCHIVED = "show_archived_sessions"
KV_LAST_OPTIONS = "last_options"
SIDEBAR_RELOAD_DELAY_MS = 150  # several sessions can report at the same moment
MAX_RESUMED_SESSIONS = 20  # sessions that were busy when the app started are picked up again, up to this many

_TOKEN_SOURCE_TEXT = {
    SOURCE_ENV: "from the environment",
    SOURCE_GUI: "set in the GUI",
    SOURCE_PROVISIONED: "provisioned by the manager",
}
_STATE_TEXT = {
    states.CONNECTING: "● connecting…",
    states.CONNECTED: "● connected",
    states.OFFLINE: "● backend unreachable",
    states.UNAUTHORIZED: "● token rejected",
    states.NO_TOKEN: "● no token",
}


SHORTCUTS = (  # what the "Keyboard shortcuts" window lists: (keys, what it does)
    ("Ctrl+N", "New session"),
    ("Ctrl+K", "Go to the list of sessions"),
    ("Ctrl+L", "Go to the message box"),
    ("Ctrl+End", "Jump to the latest message"),
    ("Ctrl+Enter", "Send the message (it is queued while the model works)"),
    ("Enter", "New line in the message"),
    ("Esc", "Stop the model, and the commands that it is running"),
    ("Ctrl+,", "Connection settings"),
    ("Ctrl+Q", "Quit"),
)


def connection_summary(settings: Settings) -> str:
    """One line for the status bar: where we connect and where the token comes from."""
    if not settings.has_token:
        token = "no token configured"
    else:
        token = "token " + _TOKEN_SOURCE_TEXT.get(settings.source_of(KEY_TOKEN), "configured")
    return f"Backend: {settings.backend_url}  |  {token}"


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, store: SessionStore | None = None, bridge: BackendBridge | None = None,
                 parent: QWidget | None = None, tools: ToolRunner | None = None, config: ConfigStore | None = None,
                 image_loader: ImageLoader | None = None):
        super().__init__(parent)
        self._settings = settings
        self._config = config  # where the connection settings are saved. Without it they cannot be changed here
        self._image_loader = image_loader
        self._offered_settings = False
        self._store = store
        self._bridge = bridge
        self._tools = tools  # runs the tool calls of the model. Without it, sessions can only report them
        self._resumed = False
        self._awaiting_models = False
        self._show_archived = bool(store.kv_get(KV_SHOW_ARCHIVED, False)) if store is not None else False
        self._shown_id: str | None = None
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*DEFAULT_SIZE)

        self._sidebar = SessionListWidget()
        self._sidebar.setObjectName("sidebar")
        self._placeholder = QLabel(
            f"<h2>etcode</h2><p>No session open.</p><p><small>version {__version__}</small></p>")
        self._placeholder.setObjectName("placeholder")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setTextFormat(Qt.TextFormat.RichText)
        self._placeholder.setWordWrap(True)
        self._pane = ChatPane()
        self._pane.setObjectName("chatPane")
        self._pane.set_image_loader(image_loader)
        self._detail = QStackedWidget()
        self._detail.addWidget(self._placeholder)
        self._detail.addWidget(self._pane)
        self._chats: dict[str, ChatSession] = {}
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(SIDEBAR_RELOAD_DELAY_MS)
        self._reload_timer.timeout.connect(self.reload_sessions)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self._sidebar)
        self._splitter.addWidget(self._detail)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([280, DEFAULT_SIZE[0] - 280])
        self._splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self._splitter)

        self._connection_label = QLabel(connection_summary(settings))
        self._connection_label.setObjectName("connectionLabel")
        self._state_label = QLabel()
        self._state_label.setObjectName("stateLabel")
        self._retry_button = QPushButton("Retry now")
        self._retry_button.setObjectName("retryConnectionButton")
        self._retry_button.setFlat(True)
        self._retry_button.setVisible(False)
        self._retry_button.clicked.connect(self.retry_connection)
        self._countdown = QTimer(self)  # keeps "retry in N s" up to date while the backend does not answer
        self._countdown.setInterval(500)
        self._countdown.timeout.connect(self._update_state_text)
        self._settings_button = QPushButton("Connection settings…")
        self._settings_button.setObjectName("connectionSettingsButton")
        self._settings_button.setFlat(True)
        self._settings_button.setVisible(False)
        self._settings_button.clicked.connect(self.open_connection_settings)
        self._gear = QToolButton()
        self._gear.setObjectName("gearButton")
        self._gear.setText("⚙")
        self._gear.setToolTip("Connection settings")
        self._gear.setEnabled(config is not None)
        self._gear.clicked.connect(self.open_connection_settings)
        self.statusBar().addWidget(self._state_label)
        self.statusBar().addWidget(self._retry_button)
        self.statusBar().addWidget(self._settings_button)
        self.statusBar().addPermanentWidget(self._connection_label)
        self.statusBar().addPermanentWidget(self._gear)
        self._make_menus()

        self._sidebar.session_selected.connect(self._on_session_selected)
        self._sidebar.new_session_requested.connect(self.request_new_session)
        self._sidebar.attach_requested.connect(self.request_attach)
        self._sidebar.rename_requested.connect(self._on_rename)
        self._sidebar.archive_requested.connect(self._on_archive)
        self._sidebar.show_archived_changed.connect(self._on_show_archived_changed)

        if bridge is not None:
            bridge.sessions_changed.connect(self.reload_sessions)
            bridge.scope_changed.connect(self._on_scope_changed)
            bridge.connection_changed.connect(self._on_connection_changed)
            bridge.reconnected.connect(self._recover_chats)
            bridge.models_loaded.connect(self._on_models_loaded)
            bridge.session_created.connect(self._on_session_added)
            bridge.session_attached.connect(self._on_session_added)
            bridge.action_failed.connect(self._show_error)
            self._on_connection_changed(bridge.state, bridge.state_message)
        else:
            self._sidebar.set_new_enabled(False)
            self._state_label.setText("")
        if store is not None:
            self._restore_layout()
            self._sidebar.set_show_archived(self._show_archived)
        self.reload_sessions()
        self._show_session(None)

    # --- menus ---------------------------------------------------------------------------------------------

    def _action(self, menu, text: str, shortcut: str, slot, name: str) -> QAction:
        action = QAction(text, self)
        action.setObjectName(name)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        action.triggered.connect(lambda _checked=False: slot())
        menu.addAction(action)
        return action

    def _make_menus(self) -> None:
        bar = self.menuBar()
        bar.setObjectName("menuBar")
        session = bar.addMenu("&Session")
        self._action(session, "&New session", "Ctrl+N", self.request_new_session, "newSessionAction")
        self._action(session, "&Add a session by its id…", "", self.request_attach, "attachSessionAction")
        session.addSeparator()
        self._settings_action = self._action(session, "&Connection settings…", "Ctrl+,", self.open_connection_settings, "connectionSettingsAction")
        self._settings_action.setEnabled(self._config is not None)
        session.addSeparator()
        self._action(session, "&Quit", "Ctrl+Q", self.close, "quitAction")
        view = bar.addMenu("&View")
        self._action(view, "Go to the list of &sessions", "Ctrl+K", self.focus_sessions, "focusSessionsAction")
        self._action(view, "Go to the &message box", "Ctrl+L", self.focus_message, "focusMessageAction")
        self._action(view, "Jump to the &latest message", "Ctrl+End", self.jump_to_latest, "jumpToLatestAction")
        help_menu = bar.addMenu("&Help")
        self._action(help_menu, "&Keyboard shortcuts", "", self.show_shortcuts, "shortcutsAction")
        self._action(help_menu, "&About etcode", "", self.show_about, "aboutAction")

    def focus_sessions(self) -> None:
        self._sidebar.focus_list()

    def focus_message(self) -> None:
        if self._shown_id is not None:
            self._pane.composer.focus_input()

    def jump_to_latest(self) -> None:
        self._pane.transcript.scroll_to_bottom()

    def show_shortcuts(self) -> None:
        rows = "".join(f"<tr><td><b>{keys}</b>&nbsp;&nbsp;</td><td>{what}</td></tr>" for keys, what in SHORTCUTS)
        QMessageBox.information(self, "Keyboard shortcuts", f"<table>{rows}</table>")

    def show_about(self) -> None:
        QMessageBox.about(self, "About etcode", f"<h3>etcode {__version__}</h3><p>The window of an etcode VM: it talks to the "
                          "backend, and runs the tools that the model asks for.</p>")

    # --- connection settings -----------------------------------------------------------------------------------

    def open_connection_settings(self) -> bool:
        """Show the dialog. If it was saved, the new settings are used at once. Returns True if they were saved."""
        if self._config is None:
            return False
        dialog = ConnectionDialog(self._settings, self._config, parent=self)
        if dialog.exec() != ConnectionDialog.DialogCode.Accepted:
            return False
        self.apply_settings(self._config.load())
        return True

    def apply_settings(self, settings: Settings) -> None:
        """Use new settings: another address or token means another list of sessions (the old ones are kept, but not shown)."""
        register_secret(settings.token)
        self._settings = settings
        self._connection_label.setText(connection_summary(settings))
        self._offered_settings = False
        if self._bridge is not None:
            self._bridge.reconfigure(settings)

    def prompt_for_missing_token(self) -> bool:
        """On startup: a VM that was made without a token has none, and nothing works without one. Ask for it."""
        if self._config is not None and not self._settings.has_token:
            return self.open_connection_settings()
        return False

    def _offer_connection_settings(self) -> None:
        if self._config is None:
            return
        answer = QMessageBox.question(self, "etcode", "The backend rejected the access token.\n\nOpen the connection settings to change it?")
        if answer == QMessageBox.StandardButton.Yes:
            self.open_connection_settings()

    # --- accessors -------------------------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def sidebar(self) -> SessionListWidget:
        return self._sidebar

    @property
    def shown_session_id(self) -> str | None:
        return self._shown_id

    def bring_to_front(self) -> None:
        """Show the window even if it is minimized or behind others."""
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    # --- the list ------------------------------------------------------------------------------------

    def _records(self) -> list[SessionRecord]:
        if self._store is None or self._bridge is None:
            return []
        return self._store.list(self._bridge.scope, include_archived=self._show_archived)

    def reload_sessions(self) -> None:
        self._sidebar.set_records(self._records())
        self._update_title()

    def _update_title(self) -> None:
        """'etcode', or 'etcode: <title of the session that is shown>'."""
        title = WINDOW_TITLE
        if self._shown_id is not None and self._store is not None and self._bridge is not None:
            record = self._store.get(self._bridge.scope, self._shown_id)
            if record is not None and record.title.strip():
                title = f"{WINDOW_TITLE}: {record.title.strip()[:60]}"
        self.setWindowTitle(title)

    def _on_scope_changed(self) -> None:
        self._resumed = False
        self._close_chats()
        self._shown_id = None
        self._show_session(None)
        self._connection_label.setText(connection_summary(self._bridge.settings))

    def _on_session_added(self, session_id: str) -> None:
        self.reload_sessions()
        self._sidebar.select(session_id)

    def _on_session_selected(self, session_id: str | None) -> None:
        self._shown_id = session_id
        if session_id is not None and self._store is not None and self._bridge is not None:
            self._store.mark_opened(self._bridge.scope, session_id)
        self._show_session(session_id)

    def _show_session(self, session_id: str | None) -> None:
        """Show the conversation of a session, or the placeholder. Other sessions keep running in the background."""
        if session_id is None or self._store is None or self._bridge is None:
            self._shown_id = None
            self._pane.bind(None)
            self._detail.setCurrentWidget(self._placeholder)
            self._update_title()
            return
        self._shown_id = session_id
        self._pane.set_models(self._bridge.models)
        self._pane.bind(self._chat_for(session_id))
        self._detail.setCurrentWidget(self._pane)
        self._update_title()

    def _chat_for(self, session_id: str) -> ChatSession:
        chat = self._chats.get(session_id)
        if chat is None:
            assert self._store is not None and self._bridge is not None
            chat = ChatSession(self._bridge.client, self._store, self._bridge.scope, session_id, self, tools=self._tools, image_loader=self._image_loader)
            chat.store_updated.connect(self._reload_timer.start)
            self._chats[session_id] = chat
            chat.open()
        return chat

    def chat_for_test(self, session_id: str) -> ChatSession | None:
        return self._chats.get(session_id)

    @property
    def pane(self) -> ChatPane:
        return self._pane

    def _close_chats(self) -> None:
        for chat in self._chats.values():
            chat.close()
        self._chats.clear()
        self._pane.bind(None)

    # --- actions ---------------------------------------------------------------------------------------

    def request_new_session(self) -> None:
        if self._bridge is None or self._bridge.state == states.NO_TOKEN:
            return
        if self._bridge.models is not None:
            self._open_new_session_dialog(self._bridge.models)
        elif not self._awaiting_models:
            self._awaiting_models = True
            self._bridge.load_models()

    def _on_models_loaded(self, models: list) -> None:
        self._pane.set_models(models)
        if self._awaiting_models:
            self._awaiting_models = False
            self._open_new_session_dialog(models)

    def _open_new_session_dialog(self, models: list) -> None:
        assert self._store is not None and self._bridge is not None
        last_options = SessionOptions.from_json(self._store.kv_get(KV_LAST_OPTIONS))
        dialog = NewSessionDialog(models, self._store.kv_get(KV_LAST_MODEL), self, last_options)
        if dialog.exec() != NewSessionDialog.DialogCode.Accepted:
            return
        model = dialog.model_name()
        if model is None:
            return
        options = dialog.options()
        self._store.kv_set(KV_LAST_MODEL, model)
        self._store.kv_set(KV_LAST_OPTIONS, options.to_json())
        self._bridge.create_session(model, dialog.websearch(), options)

    def request_attach(self) -> None:
        if self._bridge is None:
            return
        text, accepted = QInputDialog.getText(self, "Add a session", "Session id:")
        if accepted and text.strip():
            self._bridge.attach_session(text.strip())

    def _on_rename(self, session_id: str, title: str) -> None:
        if self._store is not None and self._bridge is not None:
            self._store.set_title(self._bridge.scope, session_id, title)
            self.reload_sessions()

    def _on_archive(self, session_id: str, archived: bool) -> None:
        if self._store is not None and self._bridge is not None:
            self._store.set_archived(self._bridge.scope, session_id, archived)
            self.reload_sessions()

    def _on_show_archived_changed(self, shown: bool) -> None:
        self._show_archived = shown
        if self._store is not None:
            self._store.kv_set(KV_SHOW_ARCHIVED, shown)
        self.reload_sessions()

    def _resume_busy_sessions(self) -> None:
        """Sessions that the backend is working on, or that wait for tool results, were left by a previous run of
        the app. Pick them up again, so that the work goes on without anybody having to open each of them."""
        if self._store is None or self._bridge is None:
            return
        busy = [r for r in self._store.list(self._bridge.scope) if r.status in ("generating", "awaiting_tools")]
        for record in busy[:MAX_RESUMED_SESSIONS]:
            self._chat_for(record.id)

    def _on_connection_changed(self, state: str, message: str) -> None:
        if state == states.CONNECTED and not self._resumed:
            self._resumed = True
            self._resume_busy_sessions()
        self._state_label.setToolTip(message)
        self._sidebar.set_new_enabled(state != states.NO_TOKEN)
        problem = state in (states.OFFLINE, states.UNAUTHORIZED)
        self._retry_button.setVisible(problem)
        self._settings_button.setVisible(state in (states.UNAUTHORIZED, states.NO_TOKEN) and self._config is not None)
        if state == states.UNAUTHORIZED and not self._offered_settings and self._config is not None:
            self._offered_settings = True  # once for each time that the token is refused
            QTimer.singleShot(0, self._offer_connection_settings)
        elif state == states.CONNECTED:
            self._offered_settings = False
        if state == states.CONNECTED and self._bridge is not None and self._bridge.models is None:
            self._bridge.load_models(quiet=True)  # the model pickers need the catalog
        if problem:
            self._countdown.start()
        else:
            self._countdown.stop()
        self._update_state_text()

    def _update_state_text(self) -> None:
        state = self._bridge.state if self._bridge is not None else ""
        text = _STATE_TEXT.get(state, state)
        if state in (states.OFFLINE, states.UNAUTHORIZED) and self._bridge is not None:
            wait = self._bridge.seconds_until_refresh()
            if wait is not None:
                text += f" · trying again in {int(wait + 0.5)} s"
        self._state_label.setText(text)

    def retry_connection(self) -> None:
        """The user does not want to wait: ask the backend now, and let the open conversations try again at once."""
        if self._bridge is None:
            return
        self._bridge.refresh()
        for chat in self._chats.values():
            chat.retry_now()

    def _recover_chats(self) -> None:
        """The backend answers again after it had not: the conversations that were stuck pick up at once."""
        for chat in self._chats.values():
            chat.recover()

    def _show_error(self, text: str) -> None:
        self._awaiting_models = False
        QMessageBox.warning(self, "etcode", text)

    # --- window layout -------------------------------------------------------------------------------------

    def _restore_layout(self) -> None:
        assert self._store is not None
        geometry = self._store.kv_get(KV_GEOMETRY)
        if isinstance(geometry, str):
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        sizes = self._store.kv_get(KV_SPLITTER)
        if isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(n, int) and n > 0 for n in sizes):
            self._splitter.setSizes(sizes)

    def save_layout(self) -> None:
        if self._store is None:
            return
        try:
            self._store.kv_set(KV_GEOMETRY, bytes(self.saveGeometry().toBase64().data()).decode("ascii"))
            self._store.kv_set(KV_SPLITTER, self._splitter.sizes())
        except Exception:  # losing the window size is not worth a crash on exit
            log.exception("could not save the window layout")

    def shutdown(self) -> None:
        """Stop everything that runs for this window: the conversations, and with them the commands of the model.
        Safe to call more than once. Called when the window closes and when the application quits."""
        self._countdown.stop()
        self.save_layout()
        self._close_chats()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)

