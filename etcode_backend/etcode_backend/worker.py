"""Generation worker: claims queued generations and streams provider output into the DB."""
from __future__ import annotations

import concurrent.futures
import contextlib
import json
import logging
import signal
import sqlite3
import threading
import time
import uuid

from .catalog import get_model
from .config import Settings
from .db import connect, dumps, emit, history, initialize, loads, now, transaction, add_message, record_generation_cost, fmt_cost
from .errors import ProviderError
from .providers import Cancelled, CancelToken, get_provider
from .search import SearchQuery, get_search_provider
from .sessions import _advance, resolve_pending_as_cancelled
from .tools import BACKEND_TOOLS, tools_for, tool_names

log = logging.getLogger("etcode.worker")
CANCEL_NOTICE = "\n\n[Generation cancelled by user]"
FLUSH_INTERVAL = 0.05
MAX_SEARCH_RESULT_CHARS = 100_000


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stop = threading.Event()
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=settings.worker_concurrency, thread_name_prefix="gen")

    # -- main loop ------------------------------------------------------
    def run(self) -> None:
        initialize(self.settings)
        con = connect(self.settings)
        last_sweep = 0.0
        try:
            while not self.stop.is_set():
                if time.monotonic() - last_sweep > 5:
                    self.sweep(con)
                    last_sweep = time.monotonic()
                self.claim(con)
                self.stop.wait(self.settings.worker_poll_interval)
        finally:
            self.pool.shutdown(wait=False, cancel_futures=True)
            con.close()

    def claim(self, con: sqlite3.Connection) -> int:
        with self._lock:
            room = self.settings.worker_concurrency - len(self._active)
        if room <= 0:
            return 0
        claimed = 0
        for row in con.execute("SELECT id FROM generations WHERE status='queued' ORDER BY created_at LIMIT ?", (room,)).fetchall():
            if con.execute("UPDATE generations SET status='running',updated_at=? WHERE id=? AND status='queued'", (now(), row["id"])).rowcount:
                self.submit(row["id"], recovering=False)
                claimed += 1
        return claimed

    def submit(self, gid: str, recovering: bool) -> None:
        with self._lock:
            self._active.add(gid)
        self.pool.submit(self._guarded, gid, recovering)

    def sweep(self, con: sqlite3.Connection) -> None:
        """Recover generations whose worker died and purge old events."""
        cutoff = now() - self.settings.stale_generation_seconds
        for row in con.execute("SELECT id,updated_at FROM generations WHERE status='running' AND updated_at<?", (cutoff,)).fetchall():
            with self._lock:
                if row["id"] in self._active:
                    continue
            # Compare-and-swap so only one worker recovers it.
            if con.execute("UPDATE generations SET updated_at=? WHERE id=? AND status='running' AND updated_at=?",
                           (now(), row["id"], row["updated_at"])).rowcount:
                log.warning("recovering interrupted generation %s", row["id"])
                self.submit(row["id"], recovering=True)
        con.execute("DELETE FROM events WHERE generation_id IN (SELECT id FROM generations WHERE status NOT IN ('queued','running') AND updated_at<?)",
                    (now() - self.settings.event_retention_seconds,))

    # -- one generation -------------------------------------------------
    def _guarded(self, gid: str, recovering: bool) -> None:
        con = connect(self.settings)
        try:
            self.generate(con, gid, recovering)
        except Exception:  # noqa: BLE001
            log.exception("generation %s crashed", gid)
            with contextlib.suppress(Exception):
                self._finish(con, gid, "error", {"code": "internal_error", "message": "Internal error while generating", "retryable": False})
        finally:
            with self._lock:
                self._active.discard(gid)
            con.close()

    def generate(self, con: sqlite3.Connection, gid: str, recovering: bool) -> None:
        gen = con.execute("SELECT * FROM generations WHERE id=?", (gid,)).fetchone()
        sid = gen["session_id"]
        session = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        model = get_model(session["model"])
        options = loads(gen["options"])
        cancel = CancelToken()
        stop_watch = threading.Event()
        watcher = threading.Thread(target=self._watch, args=(gid, cancel, stop_watch), daemon=True)
        watcher.start()
        try:
            has_pending = con.execute("SELECT 1 FROM pending_tool_calls WHERE session_id=?", (sid,)).fetchone()
            if recovering and not has_pending:
                self._finish(con, gid, "error", {"code": "interrupted", "message": "Generation was interrupted by a backend restart", "retryable": True})
                return
            if recovering:
                self.tool_phase(con, gid, sid, session["websearch"], cancel)
                return
            if gen["cancel_requested"]:
                cancel.cancel()
            self.stream_response(con, gid, sid, session, model, options, cancel)
        finally:
            stop_watch.set()
            watcher.join(timeout=2)

    def _watch(self, gid: str, cancel: CancelToken, stop: threading.Event) -> None:
        con = connect(self.settings)
        try:
            ticks = 0
            while not stop.wait(0.25):
                ticks += 1
                row = con.execute("SELECT cancel_requested FROM generations WHERE id=?", (gid,)).fetchone()
                if row and row["cancel_requested"] and not cancel.cancelled:
                    cancel.cancel()
                if ticks % 20 == 0:  # heartbeat every ~5s
                    con.execute("UPDATE generations SET updated_at=? WHERE id=? AND status='running'", (now(), gid))
        except Exception:  # noqa: BLE001
            log.exception("watcher failed for %s", gid)
        finally:
            con.close()

    def stream_response(self, con, gid, sid, session, model, options, cancel: CancelToken) -> None:
        provider = get_provider(model.provider, self.settings)
        owner = session["owner"]

        def load_image(hash_: str):
            row = con.execute("SELECT mime FROM images WHERE hash=? AND owner=?", (hash_, owner)).fetchone()
            if row is None:
                raise ProviderError("missing_image", "A referenced image is no longer available")
            return row["mime"], (self.settings.image_dir / hash_).read_bytes()

        text: list[str] = []
        reasoning: list[str] = []
        calls: list[dict] = []
        finish_reason = "stop"
        usage: dict | None = None
        buf = {"text": [], "reasoning": []}
        last_flush = time.monotonic()

        def flush() -> None:
            nonlocal last_flush
            for kind in ("reasoning", "text"):  # reasoning normally precedes text
                if buf[kind]:
                    emit(con, gid, f"{kind}_delta", {"text": "".join(buf[kind])})
                    buf[kind] = []
            last_flush = time.monotonic()

        try:
            if cancel.cancelled:
                raise Cancelled()
            hist = history(con, sid)
            tools = tools_for(bool(session["websearch"]), model.supports_images)
            for event in provider.stream(model, hist, tools, options, load_image, cancel):
                if event.kind in ("text", "reasoning"):
                    (text if event.kind == "text" else reasoning).append(event.data)
                    buf[event.kind].append(event.data)
                    if time.monotonic() - last_flush >= FLUSH_INTERVAL:
                        flush()
                elif event.kind == "tool_call":
                    flush()
                    call = self._normalize_call(event.data, calls)
                    if call["name"] not in tool_names(tools):
                        raise ProviderError("unknown_tool", "Model requested an unsupported tool")
                    calls.append(call)
                elif event.kind == "usage":
                    usage = event.data
                elif event.kind == "finish":
                    finish_reason = event.data
            flush()
        except Cancelled:
            flush()
            partial = self._assistant("".join(text) + CANCEL_NOTICE, "".join(reasoning), [], "cancelled", None)
            self._finish(con, gid, "cancelled", None, partial=partial, usage=usage, model=model,
                         billable=bool(usage or text or reasoning or calls))
            return
        except ProviderError as exc:
            flush()
            err = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
            self._finish(con, gid, "error", err, partial=self._assistant("".join(text), "".join(reasoning), [], "error", err),
                         usage=usage, model=model, billable=bool(usage or text or reasoning or calls))
            return
        if cancel.cancelled:  # cancelled just as the stream completed
            partial = self._assistant("".join(text) + CANCEL_NOTICE, "".join(reasoning), [], "cancelled", None)
            self._finish(con, gid, "cancelled", None, partial=partial, usage=usage, model=model,
                         billable=bool(usage or text or reasoning or calls))
            return

        message = self._assistant("".join(text), "".join(reasoning), calls, "complete", None)
        with transaction(con):
            self._record_cost(con, gid, sid, usage, model, billable=True)
            idx = add_message(con, sid, message)
            for order, call in enumerate(calls):
                con.execute("INSERT INTO pending_tool_calls(session_id,call_id,call_name,arguments,call_order) VALUES (?,?,?,?,?)",
                            (sid, call["id"], call["name"], dumps(call["arguments"]), order))
            # Publish tool calls only *after* they have been committed. Clients
            # may submit results as soon as they receive the first tool_call event.
            for call in calls:
                emit(con, gid, "tool_call", {"tool_call_id": call["id"], "name": call["name"], "arguments": call["arguments"]})
            emit(con, gid, "message_complete", {"index": idx, "finish_reason": finish_reason, "message": message})
        if calls:
            self.tool_phase(con, gid, sid, session["websearch"], cancel, finish_reason=finish_reason)
        else:
            self._finish(con, gid, "complete", None, finish_reason=finish_reason)

    @staticmethod
    def _normalize_call(raw: dict, existing: list[dict]) -> dict:
        used = {c["id"] for c in existing}
        call_id = raw.get("id") or ""
        if not call_id or call_id in used:
            call_id = f"call_{uuid.uuid4().hex[:24]}"
        try:
            arguments = json.loads(raw["arguments"]) if raw["arguments"].strip() else {}
            if not isinstance(arguments, dict):
                raise ValueError
        except ValueError:
            arguments = {"_invalid_arguments": raw["arguments"]}
        return {"id": call_id, "name": raw["name"], "arguments": arguments}

    @staticmethod
    def _assistant(text: str, reasoning: str, calls: list[dict], status: str, error: dict | None) -> dict:
        return {"role": "assistant", "text": {"type": "text", "text": text}, "reasoning": {"type": "text", "text": reasoning},
                "tool_calls": calls, "status": status, "error": error}

    def _record_cost(self, con, gid, sid, usage, model, billable: bool) -> None:
        # Record before publishing the assistant message so that cost and history
        # appear together to clients. OpenRouter's usage.cost is authoritative;
        # optional catalog prices are only a fallback for other providers.
        cost = get_provider(model.provider, self.settings).extract_cost(usage or {}, model) if usage else None
        total = record_generation_cost(con, sid, gid, usage, cost, billable)
        emit(con, gid, "usage", {"cost": fmt_cost(cost) if cost is not None else None,
                                  "currency": "USD", "session_cost_total": fmt_cost(total),
                                  "source": "provider" if usage and usage.get("cost") is not None and cost is not None else "catalog" if cost is not None else "unknown",
                                  "usage": usage})

    # -- backend tools --------------------------------------------------
    def tool_phase(self, con, gid, sid, websearch_enabled, cancel: CancelToken, finish_reason: str = "tool_calls") -> None:
        """Run backend-executed tools (websearch), then complete the generation."""
        rows = con.execute("SELECT * FROM pending_tool_calls WHERE session_id=? AND result IS NULL ORDER BY call_order", (sid,)).fetchall()
        for row in rows:
            if row["call_name"] not in BACKEND_TOOLS:
                continue
            if cancel.cancelled:
                self._finish(con, gid, "cancelled", None, tool_cancel=True)
                return
            result = self._run_search(row, bool(websearch_enabled))
            body = {"role": "tool", "tool_call_id": row["call_id"], "content": [{"type": "text", "text": result}]}
            with transaction(con):
                con.execute("UPDATE pending_tool_calls SET result=? WHERE session_id=? AND call_id=?", (dumps(body), sid, row["call_id"]))
                emit(con, gid, "tool_result", {"tool_call_id": row["call_id"], "name": row["call_name"], "content": body["content"]})
        if cancel.cancelled:
            self._finish(con, gid, "cancelled", None, tool_cancel=True)
            return
        self._finish(con, gid, "complete", None, finish_reason=finish_reason)

    def _run_search(self, row, enabled: bool) -> str:
        if not enabled:
            return "Error: websearch is not enabled for this session."
        try:
            query = SearchQuery.model_validate(loads(row["arguments"]))
            results = get_search_provider(self.settings).search(query)
            text = json.dumps(results, ensure_ascii=False, indent=1)
            return text[:MAX_SEARCH_RESULT_CHARS]
        except Exception as exc:  # noqa: BLE001 - reported to the model as a tool result
            log.warning("websearch failed: %s", exc)
            detail = str(exc) if exc.__class__.__name__ in ("ValidationError", "RuntimeError") else type(exc).__name__
            return f"Error: web search failed ({detail[:500]})"

    # -- completion -----------------------------------------------------
    def _finish(self, con, gid, status: str, error: dict | None, partial: dict | None = None,
                finish_reason: str | None = None, tool_cancel: bool = False,
                usage: dict | None = None, model=None, billable: bool = False) -> None:
        """Atomically record the outcome, advance the session and emit the closing events."""
        with transaction(con):
            gen = con.execute("SELECT session_id,status FROM generations WHERE id=?", (gid,)).fetchone()
            if gen["status"] not in ("queued", "running"):
                return
            sid = gen["session_id"]
            if model is not None:
                self._record_cost(con, gid, sid, usage, model, billable)
            if partial is not None:
                idx = add_message(con, sid, partial)
                if status == "cancelled":
                    emit(con, gid, "message_complete", {"index": idx, "finish_reason": "cancelled", "message": partial})
                else:
                    emit(con, gid, "message_complete", {"index": idx, "finish_reason": "error", "message": partial})
            if tool_cancel:
                resolve_pending_as_cancelled(con, sid)
            if status == "cancelled":
                emit(con, gid, "cancelled", {"index": idx if partial is not None else None})
            elif status == "error":
                if partial is None:  # no partial output: record the failure as an assistant entry
                    entry = self._assistant("", "", [], "error", error)
                    idx = add_message(con, sid, entry)
                    emit(con, gid, "message_complete", {"index": idx, "finish_reason": "error", "message": entry})
                emit(con, gid, "error", error)
            con.execute("UPDATE generations SET status=?,updated_at=? WHERE id=?", (status, now(), gid))
            if status == "error":
                # Tool calls from a failed recovery must not leave the session blocked.
                resolve_pending_as_cancelled(con, sid)
            nxt = _advance(con, sid)
            if status == "error" and nxt is None:
                con.execute("UPDATE sessions SET status='failed',updated_at=? WHERE id=?", (now(), sid))
            emit(con, gid, "done", {"status": status if status != "complete" else ("tool_calls" if finish_reason == "tool_calls" else "complete"),
                                    "finish_reason": finish_reason, "next_generation_id": nxt})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    worker = Worker(Settings.from_env())
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop.set())
    worker.run()


if __name__ == "__main__":
    main()
