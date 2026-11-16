"""One open session in the GUI: a controller that talks to the backend, and the state that the views draw.

The controller emits events from its threads. They are handed to the GUI thread by a queued Qt
signal and applied to the :class:`~etcode_desktop.core.session_state.SessionState` there, so the
state, and everything that listens to it, only ever runs on the GUI thread.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QObject, Signal

from ..core import controller as c
from ..core.api import BackendClient
from ..core.attachments import Attachment
from ..core.options import SessionOptions
from ..core.session_state import SessionState
from ..core.store import Scope, SessionStore
from .dispatch import GuiDispatcher, dispatcher
from .image_loader import ImageLoader


class ChatSession(QObject):
    state_changed = Signal()  # status, cost or a notice changed (the transcript has its own notifications)
    store_updated = Signal()  # the session's row in the local store changed: the sidebar should reload

    def __init__(self, client: BackendClient, store: SessionStore, scope: Scope, session_id: str,
                 parent: QObject | None = None, image_loader: ImageLoader | None = None, **controller_options: Any):
        super().__init__(parent)
        self._image_loader = image_loader
        self.session_id = session_id
        self._store = store
        self._scope = scope
        stored = store.kv_get(self._options_key())
        self._options: SessionOptions | None = SessionOptions.from_json(stored) if isinstance(stored, str) else None
        self.state = SessionState()
        self.state.subscribe(self.state_changed.emit)
        self._closed = False
        self._dispatch: GuiDispatcher = dispatcher()  # created here, on the GUI thread: the controller's threads only post to it
        self._controller = c.SessionController(client, store, scope, session_id, self._from_thread, **controller_options)

    def _from_thread(self, event: c.Event) -> None:
        if not self._closed:
            self._dispatch.post(self._apply, event)

    def _apply(self, event: c.Event) -> None:
        if self._closed:
            return
        if isinstance(event, c.StoreUpdated):
            self.store_updated.emit()
        else:
            self.state.apply(event)
            if isinstance(event, c.ImagesUploaded):
                self._keep_uploaded_pictures(event)

    def _keep_uploaded_pictures(self, event: c.ImagesUploaded) -> None:
        """The pictures that were just uploaded are known here: remember them, so that they are never downloaded again."""
        entry = self.state.model.get(event.local_key)
        attachments = getattr(entry, "attachments", None)
        if self._image_loader is not None and attachments and len(attachments) <= len(event.hashes):
            for image_hash, attachment in zip(event.hashes[-len(attachments):], attachments):
                self._image_loader.prime(image_hash, attachment.data)

    # --- what the views can ask for ------------------------------------------------------------------------

    @property
    def controller(self) -> c.SessionController:
        return self._controller

    def open(self) -> None:
        self._controller.open()

    def refresh(self) -> None:
        self._controller.refresh()

    def _options_key(self) -> str:
        return f"session_options:{self._scope.backend_url}:{self._scope.token_fp}:{self.session_id}"

    @property
    def options(self) -> SessionOptions:
        """What is sent with each message: what the user chose for this session, else what the backend has for it."""
        if self._options is not None:
            return self._options
        meta = self.state.meta
        return SessionOptions.from_mapping(meta.options) if meta is not None else SessionOptions()

    def set_options(self, options: SessionOptions) -> None:
        self._options = options
        try:
            self._store.kv_set(self._options_key(), options.to_json())
        except Exception:  # the choice is kept for this run even if it cannot be saved
            pass
        self.state_changed.emit()

    def send(self, text: str, attachments: Sequence[Attachment] = (), image_hashes: Sequence[str] = (), **overrides: Any) -> str:
        """Show the message as 'sending' and send it, with the options of this session. Returns the key of the pending entry."""
        key = self.state.begin_send(text, image_hashes, attachments)
        self._controller.send(key, text, image_hashes, attachments, **{**self.options.overrides(), **overrides})
        return key

    def change_model(self, tech_name: str) -> None:
        self._controller.change_model(tech_name)

    def stop(self) -> None:
        self._controller.stop()

    def retry_now(self) -> None:
        """Do not wait for the next attempt to reach the backend."""
        self._controller.retry_now()

    def recover(self) -> None:
        """The backend answers again: pick up what was stuck."""
        self._controller.recover()

    def close(self) -> None:
        self._closed = True
        self._controller.close(wait=0.5)  # so that no thread emits into this object while it is deleted
