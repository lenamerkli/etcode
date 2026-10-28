"""SQLite connection management and schema."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from .config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, model TEXT NOT NULL,
 websearch INTEGER NOT NULL, options TEXT NOT NULL, status TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 cost_total TEXT NOT NULL DEFAULT '0', unpriced_generations INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(owner);
CREATE TABLE IF NOT EXISTS messages (
 session_id TEXT NOT NULL REFERENCES sessions(id), idx INTEGER NOT NULL,
 body TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY (session_id, idx)
);
CREATE TABLE IF NOT EXISTS inbox (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 body TEXT NOT NULL, overrides TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 created_at REAL NOT NULL, committed_idx INTEGER,
 UNIQUE(session_id, id)
);
CREATE INDEX IF NOT EXISTS inbox_session ON inbox(session_id,state,created_at);
CREATE TABLE IF NOT EXISTS generations (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 status TEXT NOT NULL, options TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 usage TEXT, cost TEXT, cost_recorded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS generations_session ON generations(session_id,status);
CREATE TABLE IF NOT EXISTS events (
 generation_id TEXT NOT NULL REFERENCES generations(id), seq INTEGER NOT NULL,
 kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(generation_id,seq)
);
CREATE TABLE IF NOT EXISTS pending_tool_calls (
 session_id TEXT NOT NULL REFERENCES sessions(id), call_id TEXT NOT NULL,
 call_name TEXT NOT NULL, arguments TEXT NOT NULL, call_order INTEGER NOT NULL,
 result TEXT, PRIMARY KEY (session_id,call_id)
);
CREATE TABLE IF NOT EXISTS images (
 hash TEXT NOT NULL, owner TEXT NOT NULL, mime TEXT NOT NULL,
 size INTEGER NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(hash,owner)
);
"""


def connect(settings: Settings) -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


# Columns added after the first release: (table, column, definition).
MIGRATIONS = [
    ("sessions", "cost_total", "TEXT NOT NULL DEFAULT '0'"),
    ("sessions", "unpriced_generations", "INTEGER NOT NULL DEFAULT 0"),
    ("generations", "usage", "TEXT"),
    ("generations", "cost", "TEXT"),
    ("generations", "cost_recorded", "INTEGER NOT NULL DEFAULT 0"),
]


def initialize(settings: Settings) -> None:
    settings.image_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.closing(connect(settings)) as con:
        con.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:  # another process migrated first
                    pass


def fmt_cost(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def record_generation_cost(con: sqlite3.Connection, session_id: str, gen_id: str, usage: dict | None,
                           cost: Decimal | None, billable: bool) -> Decimal | None:
    """Store usage on the generation and add its cost to the session total (call inside a transaction).

    ``billable`` means a provider request produced output, so cost *should* exist.
    If it does but the provider reported no cost, the generation counts as unpriced
    instead of being silently treated as free. Returns the session's new total.
    """
    gen = con.execute("SELECT cost_recorded FROM generations WHERE id=?", (gen_id,)).fetchone()
    row = con.execute("SELECT cost_total FROM sessions WHERE id=?", (session_id,)).fetchone()
    total = Decimal(row["cost_total"])
    if gen["cost_recorded"]:
        return total  # idempotent under concurrent worker recovery
    con.execute("UPDATE generations SET usage=?, cost=?, cost_recorded=1 WHERE id=?",
                (dumps(usage) if usage else None, fmt_cost(cost) if cost is not None else None, gen_id))
    if cost is not None:
        total += cost
        con.execute("UPDATE sessions SET cost_total=? WHERE id=?", (fmt_cost(total), session_id))
    elif billable:
        con.execute("UPDATE sessions SET unpriced_generations=unpriced_generations+1 WHERE id=?", (session_id,))
    return total


@contextlib.contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.rollback()
        raise
    else:
        con.commit()


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def loads(raw: str):
    return json.loads(raw)


def now() -> float:
    return time.time()


def add_message(con: sqlite3.Connection, session_id: str, body: dict) -> int:
    idx = con.execute("SELECT COALESCE(MAX(idx),-1)+1 FROM messages WHERE session_id=?", (session_id,)).fetchone()[0]
    con.execute("INSERT INTO messages VALUES (?,?,?,?)", (session_id, idx, dumps(body), now()))
    return idx


def history(con: sqlite3.Connection, session_id: str) -> list[dict]:
    return [loads(r["body"]) for r in con.execute("SELECT body FROM messages WHERE session_id=? ORDER BY idx", (session_id,))]


def emit(con: sqlite3.Connection, gen_id: str, kind: str, payload: dict) -> int:
    seq = con.execute("SELECT COALESCE(MAX(seq),-1)+1 FROM events WHERE generation_id=?", (gen_id,)).fetchone()[0]
    con.execute("INSERT INTO events VALUES (?,?,?,?,?)", (gen_id, seq, kind, dumps({"generation_id": gen_id, "sequence": seq, **payload}), now()))
    return seq
