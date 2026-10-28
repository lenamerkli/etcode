"""Durable session state machine and inbox."""
from __future__ import annotations

import sqlite3
import uuid
from decimal import Decimal
from typing import Any

from .catalog import get_model
from .db import add_message, dumps, history, loads, now, transaction
from .errors import APIError
from .media import validate_content
from .prompts import default_system_prompt
from .schemas import SessionCreate, SubmitBatch


def _uuid() -> str:
    return uuid.uuid4().hex


def owned_session(con: sqlite3.Connection, sid: str, owner: str):
    row = con.execute("SELECT * FROM sessions WHERE id=? AND owner=?", (sid, owner)).fetchone()
    if row is None:
        raise APIError("session_not_found", "Session not found", 404)
    return row


def session_metadata(con: sqlite3.Connection, row) -> dict:
    sid = row["id"]
    count = con.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)).fetchone()[0]
    queued = con.execute("SELECT COUNT(*) FROM inbox WHERE session_id=? AND state='queued'", (sid,)).fetchone()[0]
    active = con.execute("SELECT id FROM generations WHERE session_id=? AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1", (sid,)).fetchone()
    return {"id": sid, "model": row["model"], "websearch": bool(row["websearch"]),
            "options": loads(row["options"]), "status": row["status"], "message_count": count,
            "queued_count": queued, "active_generation_id": active["id"] if active else None,
            "cost": {"currency": "USD", "total": float(Decimal(row["cost_total"])), "total_exact": row["cost_total"],
                     "unpriced_generations": row["unpriced_generations"]},
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def create_session(con: sqlite3.Connection, owner: str, request: SessionCreate) -> dict:
    if get_model(request.model) is None:
        raise APIError("unknown_model", "Unknown model", 400)
    sid = _uuid()
    options = request.model_dump(include={"temperature", "max_tokens", "reasoning_effort"}, exclude_none=True)
    with transaction(con):
        con.execute("INSERT INTO sessions(id,owner,model,websearch,options,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (sid, owner, request.model, int(request.websearch), dumps(options), "idle", now(), now()))
        add_message(con, sid, {"role": "system", "content": [{"type": "text", "text": default_system_prompt()}]})
        return session_metadata(con, owned_session(con, sid, owner))


def change_model(con: sqlite3.Connection, sid: str, owner: str, tech_name: str) -> dict:
    model = get_model(tech_name)
    if model is None:
        raise APIError("unknown_model", "Unknown model", 400)
    with transaction(con):
        row = owned_session(con, sid, owner)
        old = get_model(row["model"])
        if old.supports_images != model.supports_images:
            raise APIError("incompatible_model", "Cannot change between vision and non-vision models", 409)
        if row["status"] not in ("idle", "failed"):
            raise APIError("session_busy", "Cannot change model during generation or tool-call chain", 409)
        con.execute("UPDATE sessions SET model=?,updated_at=? WHERE id=?", (tech_name, now(), sid))
        return session_metadata(con, owned_session(con, sid, owner))


def _generation(con: sqlite3.Connection, sid: str, options: dict) -> str:
    gid = _uuid()
    con.execute("INSERT INTO generations(id,session_id,status,options,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (gid, sid, "queued", dumps(options), now(), now()))
    con.execute("UPDATE sessions SET status='generating', updated_at=? WHERE id=?", (now(), sid))
    return gid


def _effective(row, overrides: dict) -> dict:
    return {**loads(row["options"]), **overrides}


def _commit_inbox(con: sqlite3.Connection, item) -> int:
    idx = add_message(con, item["session_id"], loads(item["body"]))
    con.execute("UPDATE inbox SET state='committed',committed_idx=? WHERE id=?", (idx, item["id"]))
    return idx


def _advance(con: sqlite3.Connection, sid: str) -> str | None:
    """Advance after completion/cancellation/received tool results (in a transaction)."""
    row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if con.execute("SELECT 1 FROM generations WHERE session_id=? AND status IN ('queued','running') LIMIT 1", (sid,)).fetchone():
        return None
    pending = con.execute("SELECT * FROM pending_tool_calls WHERE session_id=? ORDER BY call_order", (sid,)).fetchall()
    if pending:
        if any(p["result"] is None for p in pending):
            con.execute("UPDATE sessions SET status='awaiting_tools',updated_at=? WHERE id=?", (now(), sid))
            return None
        last = con.execute("SELECT options FROM generations WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
        options = loads(last["options"]) if last else {}
        for p in pending:
            result = loads(p["result"])
            options.update(result.get("_overrides", {}))
            result.pop("_overrides", None)
            add_message(con, sid, result)
        con.execute("DELETE FROM pending_tool_calls WHERE session_id=?", (sid,))
        return _generation(con, sid, _effective(row, options))
    items = con.execute("SELECT * FROM inbox WHERE session_id=? AND state='queued' ORDER BY created_at,id", (sid,)).fetchall()
    if items:
        options = {}
        for item in items:
            if loads(item["body"])["role"] != "user":
                continue  # unexpected tool result never starts a user generation
            _commit_inbox(con, item)
            options.update(loads(item["overrides"]))
        return _generation(con, sid, _effective(row, options))
    con.execute("UPDATE sessions SET status='idle',updated_at=? WHERE id=?", (now(), sid))
    return None


def submit(con: sqlite3.Connection, sid: str, owner: str, request: SubmitBatch) -> dict:
    with transaction(con):
        row = owned_session(con, sid, owner)
        responses: list[dict] = []
        overrides = request.overrides()
        for msg in request.messages:
            validate_content(con, owner, msg.content)
            body = msg.model_dump()
            if msg.role == "tool":
                pending = con.execute("SELECT * FROM pending_tool_calls WHERE session_id=? AND call_id=?", (sid, msg.tool_call_id)).fetchone()
                if pending is None:
                    raise APIError("unknown_tool_call", "Tool call is not awaiting a result", 409)
                if pending["call_name"] == "websearch":
                    raise APIError("backend_tool", "websearch is executed by the backend", 409)
                if pending["result"] is not None:
                    if loads(pending["result"])["content"] != body["content"]:
                        raise APIError("duplicate_tool_result", "Conflicting result for this tool call", 409)
                    responses.append({"tool_call_id": msg.tool_call_id, "duplicate": True})
                    continue
                body["_overrides"] = overrides
                con.execute("UPDATE pending_tool_calls SET result=? WHERE session_id=? AND call_id=?",
                            (dumps(body), sid, msg.tool_call_id))
                responses.append({"tool_call_id": msg.tool_call_id, "accepted": True})
            else:
                qid = _uuid()
                con.execute("INSERT INTO inbox(id,session_id,body,overrides,state,created_at) VALUES (?,?,?,?,?,?)",
                            (qid, sid, dumps(body), dumps(overrides), "queued", now()))
                responses.append({"queue_id": qid, "queued": True})
        gid = _advance(con, sid)
        if gid:
            # Message entries from this submission may already be committed.
            for response in responses:
                if "queue_id" in response:
                    item = con.execute("SELECT committed_idx FROM inbox WHERE id=?", (response["queue_id"],)).fetchone()
                    if item["committed_idx"] is not None:
                        response["index"] = item["committed_idx"]
                        response["queued"] = False
        return {"accepted": responses, "generation_id": gid,
                "session": session_metadata(con, owned_session(con, sid, owner))}


CANCELLED_TOOL_TEXT = "Tool call was cancelled before a result was received."


def resolve_pending_as_cancelled(con: sqlite3.Connection, sid: str) -> None:
    """Commit a cancelled result for every unresolved call and drop the barrier (in a transaction)."""
    for p in con.execute("SELECT * FROM pending_tool_calls WHERE session_id=? ORDER BY call_order", (sid,)).fetchall():
        if p["result"] is None:
            body = {"role": "tool", "tool_call_id": p["call_id"], "content": [{"type": "text", "text": CANCELLED_TOOL_TEXT}]}
        else:
            body = loads(p["result"])
            body.pop("_overrides", None)
        add_message(con, sid, body)
    con.execute("DELETE FROM pending_tool_calls WHERE session_id=?", (sid,))


def cancel(con: sqlite3.Connection, sid: str, owner: str) -> dict:
    """Cancel the running generation or abandon outstanding tool calls.

    Abandoning tool calls is an addition to the original plan: without it a
    client that never answers would block the session (and queued messages) forever.
    """
    with transaction(con):
        owned_session(con, sid, owner)
        gen = con.execute("SELECT id FROM generations WHERE session_id=? AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1", (sid,)).fetchone()
        if gen is not None:
            con.execute("UPDATE generations SET cancel_requested=1,updated_at=updated_at WHERE id=?", (gen["id"],))
            return {"cancelled": "generation", "generation_id": gen["id"]}
        if con.execute("SELECT 1 FROM pending_tool_calls WHERE session_id=? LIMIT 1", (sid,)).fetchone():
            resolve_pending_as_cancelled(con, sid)
            gid = _advance(con, sid)
            return {"cancelled": "tool_calls", "generation_id": gid}
        raise APIError("no_generation", "Nothing to cancel", 409)
