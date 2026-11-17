"""The local session store: which sessions this VM has created or explicitly added.

Rows are keyed by ``(backend_url, VM identity, session id)``. The backend still checks bearer
-token authorization; this local scope determines which visible chats are remembered by this VM.
Token rotation therefore does not change the local list. The token itself is never stored.

The file is SQLite (WAL mode) with mode 0600. Every thread gets its own connection, and
writes are serialised. A database that can't be read is moved aside and replaced by an
empty one. Sessions not present in the local store can be added explicitly by ID, provided
the backend authorizes the current token.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import token_fingerprint
from .models import SessionMeta

log = logging.getLogger(__name__)

STATUS_MISSING = "missing"  # kept locally: the backend answered 404 for this session

# states of a journal entry
JOURNAL_RUNNING = "running"
JOURNAL_DONE = "done"
JOURNAL_POSTED = "posted"
JOURNAL_KEEP_SECONDS = 7 * 86400  # finished entries are kept this long, then pruned

# Each entry upgrades the schema from version n to n + 1 (``PRAGMA user_version``).
# Never edit an entry that was released. Append a new one.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE sessions (
      id              TEXT NOT NULL,
      backend_url     TEXT NOT NULL,
      token_fp        TEXT NOT NULL,
      title           TEXT NOT NULL DEFAULT '',   -- last known title: the server's, or local
      title_custom    INTEGER NOT NULL DEFAULT 0, -- 1: the user renamed it, so the server's title is ignored
      model           TEXT,
      status          TEXT,
      cost_total      TEXT,                       -- the exact decimal string from the backend
      created_at      REAL NOT NULL,
      updated_at      REAL NOT NULL,              -- last activity, as reported by the backend
      last_opened     REAL,
      last_seen_index INTEGER,                    -- highest message index that was shown
      archived        INTEGER NOT NULL DEFAULT 0, -- local only: the backend cannot delete sessions
      PRIMARY KEY (backend_url, token_fp, id)
    );
    CREATE INDEX sessions_scope_updated ON sessions(backend_url, token_fp, updated_at);
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    # 2: the tool journal. What was done with each tool call, so that a restart never runs a command twice
    """
    CREATE TABLE tool_journal (
      backend_url TEXT NOT NULL,
      token_fp    TEXT NOT NULL,
      session_id  TEXT NOT NULL,
      call_id     TEXT NOT NULL,
      name        TEXT NOT NULL,
      arguments   TEXT NOT NULL,                  -- JSON
      state       TEXT NOT NULL,                  -- 'running', 'done' (has a result, not posted yet) or 'posted'
      result      TEXT,                           -- JSON: the content parts, exactly as they are posted
      started_at  REAL NOT NULL,
      finished_at REAL,
      PRIMARY KEY (backend_url, token_fp, session_id, call_id)
    );
    """,
    # 3: the journal does not depend on the token. A session id is unique, and a call that was run for it must not
    # be run again because the token was changed (rotated, say) while the call waited. Where one call was
    # journaled under two tokens, the entry that is furthest along is kept.
    """
    CREATE TABLE tool_journal_new (
      backend_url TEXT NOT NULL,
      session_id  TEXT NOT NULL,
      call_id     TEXT NOT NULL,
      name        TEXT NOT NULL,
      arguments   TEXT NOT NULL,
      state       TEXT NOT NULL,
      result      TEXT,
      started_at  REAL NOT NULL,
      finished_at REAL,
      PRIMARY KEY (backend_url, session_id, call_id)
    );
    INSERT INTO tool_journal_new
      SELECT backend_url, session_id, call_id, name, arguments, state, result, started_at, finished_at FROM (
        SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY backend_url, session_id, call_id
                    ORDER BY CASE state WHEN 'posted' THEN 3 WHEN 'done' THEN 2 ELSE 1 END DESC, started_at) AS rank
        FROM tool_journal) WHERE rank = 1;
    DROP TABLE tool_journal;
    ALTER TABLE tool_journal_new RENAME TO tool_journal;
    """,
)
SCHEMA_VERSION = len(MIGRATIONS)


class StoreError(Exception):
    """The store can't be used (for example, it was written by a newer version of the app)."""


@dataclass(frozen=True)
class Scope:
    backend_url: str
    token_fp: str

    @classmethod
    def for_token(cls, backend_url: str, token: str) -> Scope:
        """Legacy scope helper retained for callers/tests without a stable VM identity."""
        return cls(backend_url.rstrip("/"), token_fingerprint(token))

    @classmethod
    def for_vm(cls, backend_url: str, vm_id: str) -> Scope:
        """A local chat-list scope that survives bearer-token rotation on the same VM."""
        if not vm_id or not isinstance(vm_id, str):
            raise ValueError("a stable VM id is required")
        return cls(backend_url.rstrip("/"), "vm:" + vm_id)


@dataclass(frozen=True)
class SessionRecord:
    id: str
    backend_url: str
    token_fp: str
    title: str
    title_custom: bool
    model: str | None
    status: str | None
    cost_total: str | None
    created_at: float
    updated_at: float
    last_opened: float | None
    last_seen_index: int | None
    archived: bool

    @property
    def is_missing(self) -> bool:
        return self.status == STATUS_MISSING

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SessionRecord:
        return cls(
            id=row["id"], backend_url=row["backend_url"], token_fp=row["token_fp"], title=row["title"],
            title_custom=bool(row["title_custom"]), model=row["model"], status=row["status"],
            cost_total=row["cost_total"], created_at=row["created_at"], updated_at=row["updated_at"],
            last_opened=row["last_opened"], last_seen_index=row["last_seen_index"], archived=bool(row["archived"]),
        )


@dataclass(frozen=True)
class JournalEntry:
    call_id: str
    name: str
    arguments: dict[str, Any]
    state: str
    result: list[dict[str, Any]] | None
    started_at: float
    finished_at: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> JournalEntry:
        return cls(row["call_id"], row["name"], json.loads(row["arguments"]), row["state"],
                   json.loads(row["result"]) if row["result"] is not None else None, row["started_at"], row["finished_at"])


@dataclass(frozen=True)
class MergeResult:
    imported: tuple[str, ...]  # ids that were not known before
    updated: tuple[str, ...]  # ids that were known


class SessionStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._open_or_replace()

    # --- connections and schema ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise StoreError("the store is closed")
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._new_connection()
            self._local.connection = connection
            with self._connections_lock:
                self._connections.append(connection)
        return connection

    def _new_connection(self) -> sqlite3.Connection:
        if not self.path.exists():
            # Create the file ourselves, so that it is private from the start. SQLite gives the
            # -wal and -shm files the mode of the main file.
            os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)  # we manage transactions
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _open_or_replace(self) -> None:
        try:
            self._migrate()
        except StoreError:
            raise
        except sqlite3.DatabaseError as exc:
            if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
                raise
            self._set_aside(exc)
            self._migrate()

    def _set_aside(self, reason: Exception) -> None:
        self._drop_connections()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        log.warning("the session store %s is unreadable (%s). Moving it to %s and starting an empty one. "
                    "The sessions are imported from the backend again.", self.path, reason, aside.name)
        os.replace(self.path, aside)
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(str(self.path) + suffix)
            except FileNotFoundError:
                pass

    def _migrate(self) -> None:
        connection = self._connect()
        with self._write_lock:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StoreError(f"{self.path} was written by a newer version of etcode_desktop "
                                 f"(schema {version}, this version knows {SCHEMA_VERSION})")
            for number in range(version, SCHEMA_VERSION):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _statements(MIGRATIONS[number]):
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={number + 1}")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                connection.execute("COMMIT")
            # A file that is not a database fails here at the latest, before anything is trusted.
            connection.execute("SELECT COUNT(*) FROM sessions").fetchone()

    def _drop_connections(self) -> None:
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    def close(self) -> None:
        self._closed = True
        self._drop_connections()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        with self._write_lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    # --- sessions -------------------------------------------------------------------------

    def migrate_scope(self, old: Scope, new: Scope) -> int:
        """Copy locally remembered sessions from the former token scope into a VM scope.

        This one-time compatibility path keeps chats already stored by pre-VM-identity versions
        visible after upgrade. Existing rows are retained in the old scope as a rollback aid.
        """
        if old == new:
            return 0
        with self._write() as connection:
            before = connection.total_changes
            connection.execute(
                "INSERT OR IGNORE INTO sessions(id,backend_url,token_fp,title,title_custom,model,status,cost_total,"
                "created_at,updated_at,last_opened,last_seen_index,archived) "
                "SELECT id,backend_url,?,title,title_custom,model,status,cost_total,created_at,updated_at,"
                "last_opened,last_seen_index,archived FROM sessions WHERE backend_url=? AND token_fp=?",
                (new.token_fp, old.backend_url, old.token_fp),
            )
            return connection.total_changes - before

    def get(self, scope: Scope, session_id: str) -> SessionRecord | None:
        row = self._connect().execute(
            "SELECT * FROM sessions WHERE backend_url=? AND token_fp=? AND id=?",
            (scope.backend_url, scope.token_fp, session_id)).fetchone()
        return SessionRecord.from_row(row) if row else None

    def list(self, scope: Scope, *, include_archived: bool = False) -> list[SessionRecord]:
        """The scope's sessions, most recent activity first."""
        sql = "SELECT * FROM sessions WHERE backend_url=? AND token_fp=?"
        if not include_archived:
            sql += " AND archived=0"
        sql += " ORDER BY updated_at DESC, created_at DESC, id DESC"
        return [SessionRecord.from_row(r) for r in self._connect().execute(sql, (scope.backend_url, scope.token_fp))]

    def ids(self, scope: Scope, *, include_archived: bool = True) -> set[str]:
        return {r.id for r in self.list(scope, include_archived=include_archived)}

    def upsert_from_meta(self, scope: Scope, meta: SessionMeta, *, title: str | None = None) -> tuple[SessionRecord, bool]:
        """Add a session, or refresh a known one from the backend's metadata.

        Returns the record and whether it is new. ``title`` is a locally derived title for a
        session that has none yet. The backend's own title (from ``GET /sessions``) wins over
        it, and a title the user typed wins over both.
        """
        with self._write() as connection:
            created = self._upsert(connection, scope, meta, title)
        record = self.get(scope, meta.id)
        assert record is not None
        return record, created

    def merge_listing(self, scope: Scope, listing: Sequence[SessionMeta]) -> MergeResult:
        """Apply a full or partial ``GET /sessions`` result: import unknown sessions, refresh known ones."""
        imported: list[str] = []
        updated: list[str] = []
        with self._write() as connection:
            for meta in listing:
                (imported if self._upsert(connection, scope, meta, None) else updated).append(meta.id)
        return MergeResult(tuple(imported), tuple(updated))

    @staticmethod
    def _upsert(connection: sqlite3.Connection, scope: Scope, meta: SessionMeta, local_title: str | None) -> bool:
        where = (scope.backend_url, scope.token_fp, meta.id)
        exists = connection.execute(
            "SELECT 1 FROM sessions WHERE backend_url=? AND token_fp=? AND id=?", where).fetchone() is not None
        created_at = meta.created_at or time.time()
        updated_at = meta.updated_at or created_at
        if not exists:
            connection.execute(
                "INSERT INTO sessions(id,backend_url,token_fp,title,model,status,cost_total,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (meta.id, scope.backend_url, scope.token_fp, meta.title or local_title or "", meta.model, meta.status,
                 meta.cost.total_exact, created_at, updated_at))
            return True
        connection.execute(
            "UPDATE sessions SET model=?, status=?, cost_total=?, updated_at=? "
            "WHERE backend_url=? AND token_fp=? AND id=?",
            (meta.model, meta.status, meta.cost.total_exact, updated_at, *where))
        new_title = meta.title or local_title
        if new_title:
            # the user's own title is never replaced. A local title only fills a gap
            connection.execute(
                "UPDATE sessions SET title=? WHERE backend_url=? AND token_fp=? AND id=? AND title_custom=0"
                + ("" if meta.title else " AND title=''"),
                (new_title, *where))
        return False

    def unlisted(self, scope: Scope, listed_ids: Collection[str]) -> list[str]:
        """Local sessions that are not archived, not already ``missing``, and not in ``listed_ids``.

        After a *complete* listing these are the sessions to double check with ``GET /session/<id>``.
        """
        listed = set(listed_ids)
        return [r.id for r in self.list(scope) if r.id not in listed and not r.is_missing]

    def mark_missing(self, scope: Scope, session_id: str) -> bool:
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET status=? WHERE backend_url=? AND token_fp=? AND id=? AND COALESCE(status,'')<>?",
                (STATUS_MISSING, scope.backend_url, scope.token_fp, session_id, STATUS_MISSING))
            return cursor.rowcount > 0

    def set_title(self, scope: Scope, session_id: str, title: str) -> None:
        """A title typed by the user. An empty one removes the custom title (the last known one stays)."""
        title = " ".join(title.split())
        with self._write() as connection:
            if title:
                connection.execute("UPDATE sessions SET title=?, title_custom=1 WHERE backend_url=? AND token_fp=? AND id=?",
                                   (title, scope.backend_url, scope.token_fp, session_id))
            else:
                connection.execute("UPDATE sessions SET title_custom=0 WHERE backend_url=? AND token_fp=? AND id=?",
                                   (scope.backend_url, scope.token_fp, session_id))

    def set_title_if_unset(self, scope: Scope, session_id: str, title: str) -> None:
        title = " ".join(title.split())
        if title:
            with self._write() as connection:
                connection.execute("UPDATE sessions SET title=? WHERE backend_url=? AND token_fp=? AND id=? AND title=''",
                                   (title, scope.backend_url, scope.token_fp, session_id))

    def set_archived(self, scope: Scope, session_id: str, archived: bool) -> None:
        with self._write() as connection:
            connection.execute("UPDATE sessions SET archived=? WHERE backend_url=? AND token_fp=? AND id=?",
                               (int(archived), scope.backend_url, scope.token_fp, session_id))

    def mark_opened(self, scope: Scope, session_id: str, when: float | None = None) -> None:
        with self._write() as connection:
            connection.execute("UPDATE sessions SET last_opened=? WHERE backend_url=? AND token_fp=? AND id=?",
                               (time.time() if when is None else when, scope.backend_url, scope.token_fp, session_id))

    def advance_last_seen(self, scope: Scope, session_id: str, index: int) -> None:
        """Remember the highest message index that was shown. It never goes down."""
        with self._write() as connection:
            connection.execute(
                "UPDATE sessions SET last_seen_index=MAX(COALESCE(last_seen_index,-1), ?) "
                "WHERE backend_url=? AND token_fp=? AND id=?",
                (index, scope.backend_url, scope.token_fp, session_id))

    # --- the tool journal -------------------------------------------------------------------------------
    # Entries belong to a backend and a session, not to a token: a call must not be run twice because the token changed.

    def journal_start(self, scope: Scope, session_id: str, call_id: str, name: str, arguments: dict[str, Any]) -> bool:
        """Write that a call is being run. Returns False, and changes nothing, if the call is in the journal already."""
        with self._write() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO tool_journal(backend_url,session_id,call_id,name,arguments,state,started_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (scope.backend_url, session_id, call_id, name, json.dumps(arguments), JOURNAL_RUNNING, time.time()))
            return cursor.rowcount > 0

    def journal_done(self, scope: Scope, session_id: str, call_id: str, result: list[dict[str, Any]]) -> None:
        """Write the result, before it is posted. A result that is in the journal is never computed again."""
        with self._write() as connection:
            connection.execute(
                "UPDATE tool_journal SET state=?, result=?, finished_at=? "
                "WHERE backend_url=? AND session_id=? AND call_id=? AND state=?",
                (JOURNAL_DONE, json.dumps(result), time.time(), scope.backend_url, session_id, call_id, JOURNAL_RUNNING))

    def journal_posted(self, scope: Scope, session_id: str, call_id: str) -> None:
        with self._write() as connection:
            connection.execute(
                "UPDATE tool_journal SET state=? WHERE backend_url=? AND session_id=? AND call_id=? AND state=?",
                (JOURNAL_POSTED, scope.backend_url, session_id, call_id, JOURNAL_DONE))

    def journal_get(self, scope: Scope, session_id: str, call_id: str) -> JournalEntry | None:
        row = self._connect().execute(
            "SELECT * FROM tool_journal WHERE backend_url=? AND session_id=? AND call_id=?",
            (scope.backend_url, session_id, call_id)).fetchone()
        return JournalEntry.from_row(row) if row else None

    def journal_entries(self, scope: Scope, session_id: str) -> list[JournalEntry]:
        rows = self._connect().execute(
            "SELECT * FROM tool_journal WHERE backend_url=? AND session_id=? ORDER BY started_at, call_id",
            (scope.backend_url, session_id))
        return [JournalEntry.from_row(r) for r in rows]

    def journal_prune(self, keep_seconds: float = JOURNAL_KEEP_SECONDS, now: float | None = None) -> int:
        """Forget posted calls that are older than ``keep_seconds``. Calls that are not posted are never pruned."""
        cutoff = (time.time() if now is None else now) - keep_seconds
        with self._write() as connection:
            return connection.execute("DELETE FROM tool_journal WHERE state=? AND COALESCE(finished_at, started_at) < ?",
                                      (JOURNAL_POSTED, cutoff)).rowcount

    # --- small settings (window geometry, last model, ...) -----------------------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self._connect().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def kv_set(self, key: str, value: Any) -> None:
        with self._write() as connection:
            connection.execute("INSERT INTO kv(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, json.dumps(value)))

    def kv_delete(self, key: str) -> None:
        with self._write() as connection:
            connection.execute("DELETE FROM kv WHERE key=?", (key,))


def _statements(script: str) -> list[str]:
    """Split a migration script into statements (the scripts contain no semicolons inside strings)."""
    lines = (line.split("--", 1)[0] for line in script.splitlines())
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
