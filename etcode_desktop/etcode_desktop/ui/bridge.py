"""Connects the Qt window to the backend client and the session store.

The window must never wait for the network. Every backend call here runs on a short-lived
daemon thread, and its result comes back to the GUI thread as a Qt signal. Daemon threads
so that quitting the app never waits for a slow request.

Results that arrive after :meth:`BackendBridge.reconfigure` (a new token or URL) belong to
the old scope and are dropped.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from ..config import Settings
from ..core.api import BackendClient
from ..core.describe import describe_error
from ..core.errors import AuthError, BackendError, ProtocolError, ServerError, Unreachable
from ..core.models import ModelInfo
from ..core.options import SessionOptions
from ..core.store import Scope, SessionStore, StoreError
from ..core.sync import SessionSync
from .dispatch import GuiDispatcher, dispatcher

log = logging.getLogger(__name__)

# connection states
CONNECTING = "connecting"
CONNECTED = "connected"
OFFLINE = "offline"
UNAUTHORIZED = "unauthorized"
NO_TOKEN = "no_token"

REFRESH_INTERVAL_MS = 30_000
OFFLINE_REFRESH_INTERVAL_MS = 5_000  # while the backend does not answer, look more often


def state_for_error(exc: BaseException) -> str | None:
    """The connection state an error implies, or None if it says nothing about the connection."""
    if isinstance(exc, AuthError):
        return UNAUTHORIZED
    if isinstance(exc, (Unreachable, ServerError, ProtocolError)):
        return OFFLINE
    return None


class BackendBridge(QObject):
    sessions_changed = Signal()  # the store changed: reload the list
    scope_changed = Signal()  # another token or URL: the list belongs to somebody else now
    reconnected = Signal()  # the backend answers again after it had not (offline, or the token was refused)
    connection_changed = Signal(str, str)  # state, message
    models_loaded = Signal(object)  # list[ModelInfo]
    session_created = Signal(str)
    session_attached = Signal(str)
    action_failed = Signal(str)  # a user-visible error for something the user asked for

    def __init__(
        self,
        store: SessionStore,
        settings: Settings,
        *,
        make_client: Callable[[Settings], BackendClient] = BackendClient.from_settings,
        refresh_interval_ms: int = REFRESH_INTERVAL_MS,
        offline_interval_ms: int = OFFLINE_REFRESH_INTERVAL_MS,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._store = store
        self._make_client = make_client
        self._epoch = 0
        self._stopped = False
        self._syncing = False
        self._resync = False
        self._state = CONNECTING
        self._state_message = ""
        self._models: list[ModelInfo] | None = None
        self._loading_models = False
        self._quiet_models = False
        self._dispatch: GuiDispatcher = dispatcher()  # created here, on the GUI thread: worker threads only post to it
        self._normal_interval = refresh_interval_ms
        self._offline_interval = min(offline_interval_ms, refresh_interval_ms)
        self._timer = QTimer(self)
        self._timer.setInterval(refresh_interval_ms)
        self._timer.timeout.connect(self.refresh)
        self._client: BackendClient | None = None
        self._sync: SessionSync | None = None
        self._install(settings)

    # --- state ----------------------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def client(self) -> BackendClient:
        assert self._client is not None
        return self._client

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def state(self) -> str:
        return self._state

    @property
    def state_message(self) -> str:
        return self._state_message

    @property
    def models(self) -> list[ModelInfo] | None:
        return self._models

    def _install(self, settings: Settings) -> None:
        if self._client is not None:
            self._client.close()
        self._epoch += 1
        self._settings = settings
        self._scope = (Scope.for_vm(settings.backend_url, settings.vm_id) if settings.vm_id
                       else Scope.for_token(settings.backend_url, settings.token))
        if settings.vm_id and settings.token:
            # Preserve the local list created by older releases, which was keyed by token.
            self._store.migrate_scope(Scope.for_token(settings.backend_url, settings.token), self._scope)
        self._client = self._make_client(settings)
        self._sync = SessionSync(self._client, self._store, self._scope)
        self._models = None
        self._loading_models = False
        self._syncing = False
        self._resync = False

    def _set_state(self, state: str, message: str = "") -> None:
        if (state, message) != (self._state, self._state_message):
            previous = self._state
            self._state, self._state_message = state, message
            interval = self._offline_interval if state == OFFLINE else self._normal_interval
            if self._timer.interval() != interval:
                self._timer.setInterval(interval)
            self.connection_changed.emit(state, message)
            if state == CONNECTED and previous in (OFFLINE, UNAUTHORIZED):
                self.reconnected.emit()

    def seconds_until_refresh(self) -> float | None:
        """When the next automatic refresh is due, or None if there is none planned."""
        if not self._timer.isActive():
            return None
        return max(self._timer.remainingTime(), 0) / 1000

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        self._stopped = False
        self.refresh()
        self._timer.start()

    def stop(self) -> None:
        self._stopped = True
        self._timer.stop()
        if self._client is not None:
            self._client.close()

    def reconfigure(self, settings: Settings) -> None:
        """Use another URL or token. Sessions of the old scope stay in the store but are not shown."""
        changed = (settings.backend_url, settings.token) != (self._settings.backend_url, self._settings.token)
        if not changed:
            self._settings = settings  # the other settings do not concern the backend
            return
        self._install(settings)
        self._state, self._state_message = CONNECTING, ""
        self.scope_changed.emit()
        self.sessions_changed.emit()
        self.refresh()

    # --- running things on threads ---------------------------------------------------------------

    def _run(self, work: Callable[[], Any], on_ok: Callable[[Any], None], on_error: Callable[[BaseException], None]) -> None:
        epoch = self._epoch

        def target() -> None:
            try:
                result = work()
            except Exception as exc:  # reported to the GUI thread, never lost on this one
                outcome = (epoch, on_error, None, exc)
            else:
                outcome = (epoch, on_ok, result, None)
            if not self._stopped:
                self._dispatch.post(self._on_finished, *outcome)

        threading.Thread(target=target, daemon=True, name="backend-task").start()

    def _on_finished(self, epoch: int, callback: Callable[[Any], None], result: Any, error: BaseException | None) -> None:
        if epoch != self._epoch or self._stopped:
            return  # an answer for a scope or a session of the past
        callback(error if error is not None else result)

    # --- actions -------------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Bring the store up to date with the backend. Calls made while a refresh runs are merged into one more."""
        if self._stopped:
            return
        if not self._settings.has_token:
            self._set_state(NO_TOKEN, "No access token is configured.")
            return
        if self._syncing:
            self._resync = True
            return
        self._syncing = True
        if self._state in (NO_TOKEN, UNAUTHORIZED):
            self._set_state(CONNECTING)
        assert self._sync is not None
        self._run(self._sync.sync, self._on_synced, self._on_sync_failed)

    def _on_synced(self, result: Any) -> None:
        self._syncing = False
        self._set_state(CONNECTED)
        self.sessions_changed.emit()
        self._maybe_resync()

    def _on_sync_failed(self, error: BaseException) -> None:
        self._syncing = False
        self._resync = False
        state = state_for_error(error)
        if state is None:
            log.exception("session sync failed", exc_info=error)
        self._set_state(state or OFFLINE, describe_error(error))
        self.sessions_changed.emit()  # a failed sync can still have updated some sessions

    def _maybe_resync(self) -> None:
        if self._resync:
            self._resync = False
            self.refresh()

    def load_models(self, quiet: bool = False) -> None:
        """Fetch the model catalog (once at a time: asking again while it is on its way does nothing).

        A ``quiet`` fetch is one that nobody asked for (the window does it as soon as the backend answers): if it
        fails, nothing is shown. If the user asks while a quiet fetch is on its way, it counts as asked for."""
        assert self._client is not None
        if self._loading_models:
            self._quiet_models = self._quiet_models and quiet
            return
        self._loading_models = True
        self._quiet_models = quiet
        self._run(self._client.models, self._on_models, self._on_models_failed)

    def _on_models_failed(self, error: BaseException) -> None:
        self._loading_models = False
        if not self._quiet_models:
            self._on_action_failed(error)
            return
        log.info("the model catalog could not be fetched: %s", error)
        state = state_for_error(error)
        if state is not None:
            self._set_state(state, describe_error(error))

    def _on_models(self, models: list[ModelInfo]) -> None:
        self._loading_models = False
        self._models = models
        self._set_state(CONNECTED)
        self.models_loaded.emit(models)

    def create_session(self, model: str, websearch: bool = False, options: SessionOptions | None = None) -> None:
        assert self._sync is not None
        sync = self._sync
        overrides = options.overrides() if options is not None else {}
        self._run(lambda: sync.create(model, websearch=websearch, **overrides), self._on_created, self._on_action_failed)

    def _on_created(self, record: Any) -> None:
        self._set_state(CONNECTED)
        self.sessions_changed.emit()
        self.session_created.emit(record.id)

    def attach_session(self, session_id: str) -> None:
        assert self._sync is not None
        sync = self._sync
        self._run(lambda: sync.attach(session_id), self._on_attached, self._on_action_failed)

    def _on_attached(self, record: Any) -> None:
        self.sessions_changed.emit()
        self.session_attached.emit(record.id)

    def _on_action_failed(self, error: BaseException) -> None:
        state = state_for_error(error)
        if state is not None:
            self._set_state(state, describe_error(error))
        elif not isinstance(error, (BackendError, StoreError, ValueError)):
            log.exception("backend action failed", exc_info=error)
        self.action_failed.emit(describe_error(error))
