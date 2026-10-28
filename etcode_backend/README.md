# etcode-backend

The HTTP/SSE backend that etcode clients (etcode_desktop) use to talk to LLMs.
It keeps sessions in SQLite, streams model output, runs `websearch` itself, and
hands every other tool call back to the client.

* [DECISIONS.md](DECISIONS.md): agreed requirements
* [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md): design
* [PLAN.md](PLAN.md): the original sketch

## Running

Python 3.13+ is required (3.14 is preferred where available).

```bash
cd etcode_backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill in the tokens and API keys

gunicorn -c gunicorn.conf.py   # HTTP API on 0.0.0.0:8765
python -m etcode_backend       # generation worker (a separate process, required)
```

The API and the worker communicate only through the SQLite database in
`ETCODE_DATA_DIR`, so both processes must run on the same machine. Run both under
a process supervisor (systemd, supervisord, ...). For anything beyond a trusted
network, put the API behind a TLS-terminating reverse proxy. If you use nginx,
disable buffering for `/session/*/stream`. The backend already sends
`X-Accel-Buffering: no`.

Tests: `pip install -r requirements.txt && pytest`. They use a fake upstream
server and make no paid API calls.

## Authentication and ownership

Every request needs `Authorization: Bearer <token>` with a token from
`ETCODE_API_TOKENS`. Sessions and uploaded images belong to the token that
created them. Other tokens get 404.

## API

Errors look like `{"error": {"code": "...", "message": "..."}}`.

| Method & path | Purpose |
| --- | --- |
| `GET /models` | Curated catalog. `available` is false when the provider key is missing. |
| `POST /session` | `{"model": tech_name, "websearch": bool, "temperature"?, "max_tokens"?, "reasoning_effort"?}` returns metadata (201). |
| `GET /session/<id>` | Metadata only, with `status` set to `idle`, `generating`, `awaiting_tools` or `failed`, and the accumulated `cost` (see below). |
| `PATCH /session/<id>/model` | `{"model": tech_name}`. Only allowed when idle/failed and the image support of both models matches. |
| `POST /session/<id>/message` | One message, plus optional inference overrides as sibling keys. |
| `POST /session/<id>/messages` | `{"messages": [...], overrides...}`. Do not mix user messages and tool results in one batch. |
| `GET /session/<id>/messages` | Full history: `[{"index": n, "message": {...}}]`. |
| `GET /session/<id>/last/<n>` | Last n messages (n ≤ 1000) with absolute indexes. |
| `GET /session/<id>/stream` | SSE for the active generation, or 204 if there is none. |
| `POST /session/<id>/cancel` | Cancels the running generation. If none is running, abandons outstanding tool calls. Returns 409 when there is nothing to cancel. |
| `POST /media/images` | Multipart field `file` (PNG/JPEG/WebP/GIF) returns `{"hash", "media_type", "mime_type"}`. |
| `GET /media/images/<hash>` | Download an image that the same token uploaded. |

### Messages

```jsonc
{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image", "hash": "<sha256>"}]}
{"role": "tool", "tool_call_id": "call_123", "content": [{"type": "text", "text": "output"}]}
// assistant (backend-created only):
{"role": "assistant", "text": {"type": "text", "text": "..."}, "reasoning": {"type": "text", "text": "..."},
 "tool_calls": [{"id": "call_123", "name": "bash", "arguments": {"command": "ls"}}],
 "status": "complete" | "cancelled" | "error", "error": null | {"code", "message", "retryable"}}
```

Submission returns 202 right away. A user message starts a generation if the
session is idle. Otherwise it is queued (`"queued": true`) and added to the
history after the current turn (including any tool-call chain) finishes.

### Tool calls

When the model calls tools, the session goes to `awaiting_tools`. Run the client
tools and post one `tool` message per call. You can post them in any order. They
are added to the history in call order once all of them have arrived, and then
generation resumes. `websearch` runs in the backend, so do not answer it. If
websearch was the only tool called, generation resumes on its own.

For `view_image`, upload the image first and return
`{"type": "image", "hash": ...}` in the tool result. Neither provider accepts
images in tool results, so the backend sends them upstream in an extra user
message right after the tool results. This message is never stored in the
history.

### Cost

Session metadata has `"cost": {"currency": "USD", "total": 0.0123, "total_exact": "0.0123", "unpriced_generations": 0}`.
`total_exact` is the exact decimal string, so use it rather than the float for
bookkeeping. Each generation's cost is added to the session total once, when it
ends. This includes cancelled generations and generations that failed after
producing output, because the provider still charges for those.

The cost is whatever the provider reports in `usage.cost` (OpenRouter does this
on every response). If a provider reports no cost, the backend uses the optional
`price_input_per_mtok` / `price_output_per_mtok` values (USD per million tokens)
from the model's entry in `catalog.py`, if set. If there is no way to price a
generation that produced output, the backend does **not** treat it as free.
It adds one to `unpriced_generations`, so the total is then a lower bound.
Requests that fail before any output arrive are not counted. The Arena entry has
no prices set, so its cost shows up as unpriced until Arena reports a cost or you add prices.
Per-generation `usage` and `cost` are stored in the `generations` table.

### SSE events

`id: <generation_id>:<sequence>`. Every payload includes `generation_id` and
`sequence`. Events: `reasoning_delta {text}`, `text_delta {text}`,
`tool_call {tool_call_id, name, arguments}`, `tool_result` (backend websearch),
`usage {cost, currency, session_cost_total, source, usage}` (`source` is `provider`, `catalog` or `unknown`),
`message_complete {index, finish_reason, message}`, `error {code, message, retryable}`,
`cancelled {index}`, `done {status, finish_reason, next_generation_id}`.
On connect the backend replays the generation from the start. You can send
`Last-Event-ID` to resume from a later point instead. If `done` has a
`next_generation_id` (after websearch or a queued message), connect again to
follow it.

## Behaviour notes

* Retries: 408/429/5xx-type errors and network failures are retried with
  exponential backoff (at most 5 attempts or about 90 s in total), honouring
  `Retry-After`. Retries happen only before any output has been streamed, so
  output is never duplicated. Arena 403 is never retried. A context-length error
  ends the generation.
* Failures and cancellations are stored as assistant messages
  (`status: error` / `cancelled`). A cancelled message keeps the partial text
  plus a cancellation notice.
* If the worker restarts in the middle of a generation, that generation is
  recorded as an `interrupted` error instead of being run again.
* Switching search providers: implement `search(query) -> list[{title,url,description,age}]`
  in `etcode_backend/search/`, add it to the registry in
  `etcode_backend/search/__init__.py`, and set `ETCODE_SEARCH_PROVIDER`.
* Adding models: add an entry to `etcode_backend/catalog.py`.
