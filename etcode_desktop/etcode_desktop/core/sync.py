"""Keeps the local session store and the backend in step.

* :meth:`SessionSync.sync` imports the backend's sessions that the store doesn't know,
  refreshes the known ones, and marks local sessions that the backend no longer has as
  ``missing``. Without ``GET /sessions`` (an older backend) it refreshes the most recent local
  sessions one by one instead.
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
from .errors import NotFound, Unsupported
from .models import SessionMeta
from .store import Scope, SessionRecord, SessionStore, StoreError

log = logging.getLogger(__name__)

PAGE_SIZE = 500  # the backend's maximum
MAX_PAGES = 100  # a safety net against a backend that never stops
MAX_REFRESH = 50  # sessions refreshed one by one when the backend can't list them


@dataclass(frozen=True)
class SyncResult:
    supported: bool  # the backend has GET /sessions
    complete: bool  # every session of the backend was fetched (always False when unsupported)
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
        """Bring the store up to date. ``skip`` are sessions that something else is following live."""
        try:
            listing, complete = self._fetch_all()
        except Unsupported:
            return self._refresh_individually(skip)

        merged = self._store.merge_listing(self._scope, listing)
        result = {"imported": len(merged.imported), "updated": len(merged.updated), "marked_missing": 0, "refreshed": 0}
        if complete:
            # Not being listed is not proof of absence: the listing is a snapshot, and sessions can move
            # between its pages. So each one is asked about before it is called missing.
            listed = {m.id for m in listing}
            for session_id in self._store.unlisted(self._scope, listed):
                if session_id in skip:
                    continue
                result["refreshed"] += 1
                if not self._refresh_one(session_id):
                    result["marked_missing"] += 1
        return SyncResult(supported=True, complete=complete, **result)

    def _fetch_all(self) -> tuple[list[SessionMeta], bool]:
        collected: dict[str, SessionMeta] = {}
        offset = 0
        for _ in range(MAX_PAGES):
            page = self._client.list_sessions(limit=self._page_size, offset=offset)
            for meta in page.sessions:
                collected.setdefault(meta.id, meta)
            offset += len(page.sessions)
            if not page.sessions or offset >= page.total:
                return list(collected.values()), True
        log.warning("stopped listing sessions after %d pages", MAX_PAGES)
        return list(collected.values()), False

    def _refresh_individually(self, skip: Collection[str]) -> SyncResult:
        refreshed = marked = 0
        candidates = [r.id for r in self._store.list(self._scope) if r.id not in skip][:self._max_refresh]
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
