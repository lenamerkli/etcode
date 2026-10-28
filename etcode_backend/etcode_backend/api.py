"""Flask application factory; does not start the generation worker."""
from __future__ import annotations

import hmac
import re
import time
from datetime import datetime, timezone

from flask import Flask, Response, g, jsonify, request, send_file
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from .catalog import MODELS, get_model
from .config import Settings
from .db import connect, initialize, loads
from .errors import APIError
from .media import image_path, image_record, save_image
from .schemas import ModelChange, SessionCreate, SubmitBatch
from .sessions import cancel, change_model, create_session, owned_session, session_metadata, submit

_HASH = re.compile(r"^[0-9a-f]{64}$")


def _ts(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _metadata(meta: dict) -> dict:
    for field in ("created_at", "updated_at"):
        meta[field] = _ts(meta[field])
    return meta


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError("invalid_json", "Request must contain a JSON object")
    return data


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_env()
    if not settings.api_tokens:
        raise RuntimeError("Configure at least one backend API token in ETCODE_API_TOKENS")
    initialize(settings)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = settings.max_request_bytes
    app.config["ETCODE_SETTINGS"] = settings

    @app.before_request
    def authenticate():
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise APIError("unauthorized", "Bearer token required", 401)
        token = auth[len("Bearer "):]
        owner = None
        # Compare against *every* configured token in constant time.
        for valid, candidate_owner in settings.api_tokens.items():
            if hmac.compare_digest(token, valid):
                owner = candidate_owner
        if owner is None:
            raise APIError("unauthorized", "Invalid bearer token", 401)
        g.owner = owner
        g.db = connect(settings)

    @app.teardown_request
    def close_db(exc):
        con = g.pop("db", None)
        if con is not None:
            con.close()

    @app.errorhandler(APIError)
    def api_error(err: APIError):
        return jsonify({"error": {"code": err.code, "message": err.message}}), err.status

    @app.errorhandler(ValidationError)
    def validation_error(err: ValidationError):
        return jsonify({"error": {"code": "validation_error", "message": str(err)}}), 400

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(err):
        return jsonify({"error": {"code": "request_too_large", "message": "Request exceeds size limit"}}), 413

    @app.errorhandler(HTTPException)
    def http_error(err: HTTPException):
        return jsonify({"error": {"code": err.name.lower().replace(" ", "_"), "message": err.description}}), err.code

    @app.get("/models")
    def models():
        from .providers import get_provider
        return jsonify([dict(model.to_dict(), available=get_provider(model.provider, settings).configured()) for model in MODELS])

    @app.post("/session")
    def new_session():
        params = SessionCreate.model_validate(_json_body())
        if not get_model(params.model):
            raise APIError("unknown_model", "Unknown model")
        return jsonify(_metadata(create_session(g.db, g.owner, params))), 201

    @app.get("/session/<sid>")
    def get_session(sid: str):
        return jsonify(_metadata(session_metadata(g.db, owned_session(g.db, sid, g.owner))))

    @app.patch("/session/<sid>/model")
    def set_model(sid: str):
        params = ModelChange.model_validate(_json_body())
        return jsonify(_metadata(change_model(g.db, sid, g.owner, params.model)))

    @app.get("/session/<sid>/messages")
    def all_messages(sid: str):
        owned_session(g.db, sid, g.owner)
        rows = g.db.execute("SELECT idx,body FROM messages WHERE session_id=? ORDER BY idx", (sid,))
        return jsonify([{"index": r["idx"], "message": loads(r["body"])} for r in rows])

    @app.get("/session/<sid>/last/<number>")
    def last_messages(sid: str, number: str):
        owned_session(g.db, sid, g.owner)
        if not number.isdecimal() or int(number) > 1000:
            raise APIError("invalid_number", "number must be between 0 and 1000")
        rows = g.db.execute("SELECT idx,body FROM messages WHERE session_id=? ORDER BY idx DESC LIMIT ?", (sid, int(number))).fetchall()
        return jsonify([{"index": r["idx"], "message": loads(r["body"])} for r in reversed(rows)])

    @app.post("/session/<sid>/message")
    def new_message(sid: str):
        body = _json_body()
        # A single message can carry inference overrides as sibling keys, but
        # the message itself is otherwise validated strictly.
        overrides = {key: body.pop(key) for key in ("temperature", "max_tokens", "reasoning_effort") if key in body}
        return _submit(sid, {"messages": [body], **overrides})

    @app.post("/session/<sid>/messages")
    def new_messages(sid: str):
        return _submit(sid, _json_body())

    def _submit(sid: str, body: dict):
        owned_session(g.db, sid, g.owner)
        batch = SubmitBatch.model_validate(body)
        chars = sum(len(c.text) for m in batch.messages for c in m.content if c.type == "text")
        if chars > settings.max_text_chars:
            raise APIError("message_too_large", "Submitted text exceeds the size limit", 413)
        response = submit(g.db, sid, g.owner, batch)
        response["session"] = _metadata(response["session"])
        return jsonify(response), 202

    @app.post("/session/<sid>/cancel")
    def cancel_generation(sid: str):
        return jsonify(cancel(g.db, sid, g.owner)), 202

    @app.get("/session/<sid>/stream")
    def stream(sid: str):
        owned_session(g.db, sid, g.owner)
        gen = g.db.execute("SELECT id FROM generations WHERE session_id=? AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1", (sid,)).fetchone()
        if gen is None:
            return Response(status=204)
        gid = gen["id"]
        last_id = request.headers.get("Last-Event-ID")
        start = 0
        if last_id:
            parts = last_id.rsplit(":", 1)
            if len(parts) != 2 or parts[0] != gid or not parts[1].isdecimal():
                raise APIError("invalid_event_id", "Last-Event-ID does not belong to the active generation")
            start = int(parts[1]) + 1

        def events():
            # Dedicated connection: Flask's request DB connection is closed once
            # the response is returned, before this generator runs.
            con = connect(settings)
            next_seq = start
            last_keepalive = time.monotonic()
            try:
                while True:
                    rows = con.execute("SELECT seq,kind,payload FROM events WHERE generation_id=? AND seq>=? ORDER BY seq LIMIT 100", (gid, next_seq)).fetchall()
                    for row in rows:
                        next_seq = row["seq"] + 1
                        yield f"id: {gid}:{row['seq']}\nevent: {row['kind']}\ndata: {row['payload']}\n\n"
                    if rows:
                        last_keepalive = time.monotonic()
                    status = con.execute("SELECT status FROM generations WHERE id=?", (gid,)).fetchone()["status"]
                    if status not in ("queued", "running") and not rows:
                        break
                    if time.monotonic() - last_keepalive >= settings.stream_keepalive_seconds:
                        yield ": keepalive\n\n"
                        last_keepalive = time.monotonic()
                    time.sleep(settings.stream_poll_interval)
            finally:
                con.close()

        return Response(events(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    @app.post("/media/images")
    def upload_image():
        uploaded = request.files.get("file")
        if uploaded is None:
            raise APIError("missing_file", "Multipart field 'file' is required")
        content = uploaded.stream.read(settings.max_upload_bytes + 1)
        return jsonify(save_image(g.db, settings, g.owner, content)), 201

    @app.get("/media/images/<hash_>")
    def download_image(hash_: str):
        if not _HASH.fullmatch(hash_):
            raise APIError("image_not_found", "Image not found", 404)
        row = image_record(g.db, g.owner, hash_)
        path = image_path(settings, hash_)
        if not path.is_file():
            raise APIError("image_not_found", "Image not found", 404)
        return send_file(path, mimetype=row["mime"], as_attachment=False,
                         download_name=hash_, conditional=True)

    return app

