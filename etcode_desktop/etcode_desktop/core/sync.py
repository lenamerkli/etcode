"""Refreshes the VM's local session list without discovering sessions from the backend.

* :meth:`SessionSync.sync` refreshes locally known sessions one by one and marks a session
  ``missing`` only after an individual 404. It never calls `GET /sessions`.
* :meth:`SessionSync.create` creates a session and writes its id to the store **before it
  returns**, so that a crash right after creation doesn't orphan the session.
* :meth:`SessionSync.attach` adds a session by its id (for an older backend, or a session that was
  made with another client).

Errors from the backend client (``Unreachable``, ``AuthError``, ...) propagate. Whatever was
learned before the error stays in the store, and nothing is marked ``missing`` unless the
backend really answered 404.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from .api import BackendClient
from .errors import NotFound
from .store import Scope, SessionRecord, SessionStore, StoreError

log = logging.getLogger(__name__)

PAGE_SIZE = 500  # accepted as a legacy constructor default; list_sessions is not polled
MAX_REFRESH = 50  # cap automatic metadata refresh work per pass


@dataclass(frozen=True)
class SyncResult:
    supported: bool  # whether backend-list discovery was used (always False by design)
    complete: bool  # whether a complete backend listing was fetched (always False by design)
    imported: int = 0
    updated: int = 0
    marked_missing: int = 0
    refreshed: int = 0  # sessions looked at one by one


class SessionSync:
    def __init__(self, client: BackendClient, store: SessionStore, scope: Scope, *,
                 page_size: int = PAGE_SIZE, max_refresh: int = MAX_REFRESH):
        self._client = client
        self._store = store
        self._scope = scope
        self._page_size = page_size
        self._max_refresh = max_refresh

    @property
    def scope(self) -> Scope:
        return self._scope

    # --- sync ---------------------------------------------------------------------------

    def sync(self, skip: Collection[str] = ()) -> SyncResult:
        """Refresh locally recorded sessions without discovering chats from the backend.

        Session IDs are saved on creation or explicit attachment. ``GET /sessions`` remains
        supported by the backend, but desktop synchronization deliberately never calls it.
        """
        return self._refresh_individually(skip)

    def _refresh_individually(self, skip: Collection[str]) -> SyncResult:
        refreshed = marked = 0
        candidates = [r.id for r in self._store.list(self._scope)
                      if r.id not in skip and not r.is_missing][:self._max_refresh]
        for session_id in candidates:
            refreshed += 1
            if not self._refresh_one(session_id):
                marked += 1
        return SyncResult(supported=False, complete=False, marked_missing=marked, refreshed=refreshed)

    def _refresh_one(self, session_id: str) -> bool:
        """Update one session from the backend. Returns False if the backend doesn't know it (it is then marked)."""
        try:
            meta = self._client.get_session(session_id)
        except NotFound:
            self._store.mark_missing(self._scope, session_id)
            return False
        self._store.upsert_from_meta(self._scope, meta)
        return True

    # --- create and attach -----------------------------------------------------------------

    def create(self, model: str, *, websearch: bool = False, **options: Any) -> SessionRecord:
        """Create a session. Its id is in the store when this returns. ``options`` are temperature,
        max_tokens and reasoning_effort."""
        meta = self._client.create_session(model, websearch=websearch, **options)
        try:
            record, _ = self._store.upsert_from_meta(self._scope, meta)
        except (sqlite3.Error, OSError, StoreError) as exc:
            log.error("session %s was created but could not be saved: %s", meta.id, exc)
            raise StoreError(f"session {meta.id} was created on the backend, but could not be saved locally: {exc}") from exc
        return record

    def attach(self, session_id: str) -> SessionRecord:
        """Add a session by id. ``NotFound`` if the backend doesn't have it for this token."""
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("a session id is required")
        meta = self._client.get_session(session_id)
        record, _ = self._store.upsert_from_meta(self._scope, meta)
        return record
