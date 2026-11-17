# etcode-backend implementation plan (implemented, see README.md)

Read with [DECISIONS.md](DECISIONS.md) (agreed requirements), [PLAN.md](PLAN.md) (original API and tool definitions), [ARENA_API.md](ARENA_API.md) and [OPENROUTER.md](OPENROUTER.md). This document proposes implementation details; the items in **Questions to settle** are not yet agreed.

## Scope and architecture

A Flask HTTP/SSE API handles validation, authentication, media, and session state. A separate Python worker process handles queued generations and backend-only websearch. Both share one SQLite database in WAL mode and the media directory on a single host. Run the API with Gunicorn (`gthread` workers so open SSE requests do not monopolize processes); run the worker as a separate managed service. This avoids relying on daemon threads inside Gunicorn workers, which could be killed on reload, and allows cancellation and events to work across multiple API processes. One in-flight generation per session; different sessions can run concurrently. A restart recovers jobs or records a clear interrupted-generation failure, rather than silently leaving sessions busy. Not designed for several machines sharing SQLite.

Suggested initial port: `8765` (configurable); bind `0.0.0.0` as requested. Use Python 3.14 if available in the deployment environment, otherwise 3.13 (3.13 is installed in the current development container). Flask, Gunicorn, Pydantic v2, requests or httpx, python-dotenv, filetype, pytest. No dependency on etcode_manager.

## Proposed API contract

All endpoints require `Authorization: Bearer <backend token>`; compare configured tokens using constant-time comparisons, do not log them. Responses on invalid requests use JSON `{"error":{"code":"...","message":"..."}}` with appropriate HTTP status. IDs are opaque UUIDs. Time values are ISO 8601 UTC. Set explicit upload size, max message/tool-result size and request timeouts; enforce HTTPS with reverse proxy when exposed beyond trusted networks.

| Endpoint | Proposed request / response |
| --- | --- |
| `GET /models` | Curated array of model records. `tech_name` is unique; `provider_tech_name` is sent upstream. Provider-specific options are present when applicable (`openrouter_internal_provider: null` when not pinned). Do not report models without configured credentials as available without indicating that limitation. |
| `POST /session` | `{ "model": "<tech_name>", "websearch": false, "temperature": null, "max_tokens": null, "reasoning_effort": null }`; creates a session with a default system prompt; returns metadata including `id`, model, tool settings, status, and timestamps. Null inference options mean provider defaults. |
| `GET /session/<id>` | Metadata only: model and options, `status` (`idle`, `generating`, `awaiting_tools`, `queued`, `failed`, etc.), message count, queued count, active generation ID, timestamps. |
| `PATCH /session/<id>/model` | `{ "model": "<tech_name>" }`; reject if `supports_images` changes, if a generation is running, or while unresolved tool calls remain; preserve prior messages. |
| `POST /session/<id>/message` | Accept one client message (`role: user` or `tool`, with content; tool messages include `tool_call_id`). Return 202, message index if committed immediately or a queue ID if deferred, and generation/status metadata. |
| `POST /session/<id>/messages` | Accept `{ "messages": [...] }` for atomic submission of several tool results or queued user messages. The single-message route is a convenience wrapper over the same logic. Do not allow client-authored `assistant`/`system` messages through these routes. |
| `GET /session/<id>/messages` | Entire ordered committed history as `[{"index": 0, "message": {...}}, ...]`, including tool calls/results, reasoning, and cancellation/error metadata; do not include queued messages as committed history. |
| `GET /session/<id>/last/<number>` | Same entry format for last N messages with **absolute**, zero-based indexes in the full history. Validate/cap N. |
| `GET /session/<id>/stream` | SSE of the active generation, replaying retained events first, then following live updates. 204 if there is no active generation at connection time. Multiple simultaneous subscribers. |
| `POST /session/<id>/cancel` | Signal cancellation of the running generation. 202 if accepted, 409 if none is running (proposed). |
| `POST /media/images` | Multipart image upload; validate actual bytes as PNG/JPEG/WebP/GIF (and provider-specific restrictions); SHA-256 content hash; deduplicate; return `{ "hash": "...", "media_type": "image", "mime_type": "..." }`. |
| `GET /media/images/<hash>` | Stream stored image with its detected MIME type after auth; no arbitrary path input. `MediaContent` in messages references a pre-uploaded image by hash. |

Use the PLAN.md wire format as a starting point, corrected into validated Pydantic models: content items `{type: "text", text: "..."}` or `{type: "image", hash: "..."}`; user and tool `content` arrays; assistant `text: {type: "text", text: "..."}`, `reasoning: {type: "text", text: "..."}`, and `tool_calls: [{id, name, arguments}]`; tool messages have `tool_call_id` **on the message**. Require tool results to reference an outstanding call ID; validate the content hashes exist. Backward compatibility with buggy Python magic `__dict__()` methods is not needed. Keep immutable message indexes (assigned when committed) and expose an explicit `to_dict`/Pydantic serialization instead. System prompt is stored as a real system message at index 0.

Persisted SSE events carry `id: <generation_id>:<sequence>`, `event: <type>`, `data: <JSON>`; include `generation_id` and monotonically increasing `sequence` in all event payloads. Proposed events: `text_delta` `{text}`, `reasoning_delta` `{text}`, `tool_call` `{tool_call_id,name,arguments}` (only when fully assembled), `message_complete` `{index,finish_reason,message}`, `error` `{code,message,retryable}`, `cancelled` `{index}`, `done` `{status}`. One generation is one upstream assistant response; after client-supplied tool results or backend-only websearch a new generation ID is created. `message_complete` and `done` remain ordered even on failure/cancellation; `error` and `cancelled` precede `done` when relevant. Never emit provider secrets or raw tracebacks. Keep the event log long enough to support reconnects while active. On reconnect during an active generation replay from the start by default; optionally support SSE `Last-Event-ID` for incremental replay.

## State and queue rules

- Append-only committed history; separate FIFO inbox stores accepted but not-yet-committed user messages and tool results. The inbox exists to keep chronological provider history valid: an arriving user message cannot be committed between an assistant's tool calls and their matching tool results. Responses for queued messages return queue IDs; history indexes appear only after commit. Atomic SQLite transactions reserve and claim jobs and assign indexes.
- On a user submission to an idle session without outstanding calls: commit it and enqueue generation. If the last committed message is an assistant final response, the *new user message* is now last and triggers generation; an assistant message alone never triggers it. If already generating, queue the user message. When outstanding calls exist, queue user messages until all tool results have been committed and the corresponding follow-up generation has finished. This is a proposed ordering policy, to avoid interleaving unrelated requests into tool-call chains.
- At a tool-call finish, persist the assistant message with IDs and status `awaiting_tools`. The client runs every call serially in `call_order`. Client tools are posted via `/message(s)`; backend-owned tools are invoked via `POST /session/<id>/tool-calls/<call_id>/run`, which executes them with bounded timeouts and stores an ordinary tool message (including an error-as-tool-result on failure). Commit provider-visible results in tool-call order once all required calls have arrived, and do not resume until all calls are resolved. Deduplicate duplicate results by `tool_call_id` to avoid double execution; reject conflicting duplicates. `view_image` results contain an already-uploaded image hash.
- Cancel only the current upstream generation: worker checks the cancellation flag and closes its upstream HTTP stream promptly; persist a partial assistant message with `status: cancelled`, append an explicit cancellation notice to its text, and emit `cancelled`/`done`. Keep queued user messages and process them afterward unless product semantics say otherwise. If the generation already completed, report a conflict rather than rewriting history.
- On upstream failure: keep any partial response as an assistant entry with `status: error` and structured `error` metadata, or append an assistant error entry if there was no partial content. Emit `error` and `done`; leave original input in history and do not silently retry non-transient or context-limit failures. A follow-up user message can start a new generation. On worker crash, mark the active job interrupted with an error entry (no duplicate LLM invocation without a safe recovery policy).
- Protect against races via SQLite WAL, busy timeout, transactional claims, one job per session, and event sequence per generation. Heartbeat/check worker leases to detect jobs interrupted by worker termination; use SSE polling (or a cross-process notification optimization later). Do not store unbounded raw requests/responses in event logs; enforce limits/cleanup for completed generations.

## Provider adapters and model catalog

Build a provider interface for streaming chat completions, serializing message history and images, mapping tool calls/reasoning/finish reasons, cancellation, and normalizing errors/retry hints. Use explicit allowlisted parameters (`temperature`, `max_tokens`, `reasoning_effort`, `parallel_tool_calls: true`, supported tools). Pass OpenRouter `provider.order` when the catalog entry pins `openrouter_internal_provider`; omit provider constraints for null. Validate adapter support for each catalog entry; populate the initial curated list from approved entries (at least the two examples supplied, subject to confirmation of actual upstream IDs). Return `supports_images` from catalog, not live discovery.

Arena: implement its documented OpenAI-compatible `/v1/chat/completions` flow and headers from ARENA_API.md, avoiding logging API keys. OpenRouter: `/api/v1/chat/completions` with tool-call and reasoning streaming assembly. Preserve assistant tool-call IDs across requests; marshal `ToolMessage` into OpenAI-style tool results. Both accept text and supported image formats; reject audio/video/other media at validation. Use detected MIME types to create base64 data URLs for images. For OpenRouter image-bearing tool results, send a **text-only tool result** linked to its call ID and then a synthetic user message containing the image(s) immediately after the whole batch of tool results in the *upstream payload only*; persist only the original tool response and never expose the synthetic message in `/messages`. Document and test the prompt/role consequences of this workaround. For model switches preserving vision capability, verify images already in history can be encoded by the new provider.

Retries: classify HTTP 429 with `Retry-After`/provider reset hint, timeouts and selected 5xx/transport faults as transient; use capped exponential backoff with jitter and a total time/attempt budget. Never retry an Arena 403; other 4xx, invalid requests, and context errors are terminal. Never re-run a generation after visible streamed content or tool calls unless it can be proven safe; prevent duplicate user-visible output. Reasoning deltas are recorded and included in subsequent context only when the target provider supports the relevant format; include adapter-specific tests for cross-provider session changes.

Tool schemas mirror PLAN.md as Python dicts. Default bash/read/write/edit always advertised; websearch only when enabled; view_image only for vision-enabled models. Backend search requires a chosen search service and credentials; implement `websearch` server-side with request validation (query length/word count, count <= 20, offset <= 9, dates/language), timeout, normalized `{title,url,description,age}` results, and explicit errors that can be returned as tool results. No other tools execute in the backend; client runs them in its environment.

## Configuration and security

Load `.env` then environment variables (environment takes precedence); require one or more bearer tokens from a clearly documented multi-token setting, e.g. JSON array or comma-separated string; provider credentials via `ARENA_API_KEY`, `OPENROUTER_API_KEY`, and a search API key as appropriate. Keep `.env`, SQLite, media and logs out of version control; add `.env.example` and README instructions. Use configurable `ETCODE_DATA_DIR` defaulting to `etcode_backend/data/` containing `backend.sqlite3` and `images/`. Ensure private filesystem permissions, file content validation rather than trusting extensions, upload/hash validation, rate/size limits, and no local paths exposed through media API. Tokens authorize access to **all** sessions in this first version (no per-token tenancy); mark this explicitly as a security constraint for deployments. Do not expose API directly over unencrypted Internet; terminate TLS at a proxy.

## Suggested layout and implementation order

```
etcode_backend/
  DECISIONS.md, IMPLEMENTATION_PLAN.md, PLAN.md
  requirements.txt, .env.example, README.md
  etcode_backend/
    __init__.py, api.py, config.py, schemas.py, db.py, models.py
    sessions.py, worker.py, streaming.py, media.py, tools.py
    providers/{base.py, arena.py, openrouter.py}
  tests/{test_api.py, test_sessions.py, test_streaming.py,
         test_providers.py, test_media.py, test_worker.py}
```

1. Define and review wire contracts and outstanding questions below. Confirm curated models and search integration.
2. Create configuration, Pydantic schemas, SQLite migrations/tables (`sessions`, `messages`, `inbox`, `generations`, `events`, `pending_tool_calls`), auth, and catalog; add database/concurrency tests.
3. Implement session/model/message/history endpoints and media upload/download with unit and Flask integration tests.
4. Implement provider adapters and normalization: text, reasoning, image inputs, tool-call fragments, image-tool workaround, retry/error policy; test using mocked HTTP/SSE fixtures and malformed/truncated streams.
5. Implement worker lifecycle, FIFO queue, backend websearch, client tool-result barrier, durable SSE replay/fan-out, cancellation, crash recovery; test with multiple concurrent clients/API processes where possible.
6. Add default system prompt, README for Gunicorn + worker startup, env configuration, curl/SSE usage examples, and end-to-end tests using fake providers (real paid provider smoke tests only with opt-in credentials).

## Questions to settle before implementation

1. Which web search service should the backend use, and do you have a key for it? (I suggest Brave Search if its API and terms meet your needs.)
2. Are the two sample models actual initial catalog entries, or just examples? Please provide the initial curated catalog, or authorize an initial catalog consisting of those examples **after verifying their upstream IDs/capabilities**.
3. May any configured bearer token read/change **any** session, or should sessions be owned by a token (important if different clients share a server)? The plan currently assumes shared access.
4. Confirm the proposed batch `POST /session/<id>/messages` body, queued-user-message ordering around tool-call chains, and whether inference options are defaults at session creation with per-submission overrides (currently proposed but not specified by the original plan).

## Implementation notes (where the build differs from the plan above)

- The questions above were answered and recorded in DECISIONS.md.
- `POST /session/<id>/cancel` also covers outstanding tool calls when no generation is running. Each unanswered call gets a "cancelled" tool result and queued messages continue, so a client that never answers cannot block a session forever.
- `GET /models` adds an `available` flag that is false when the provider API key is missing.
- Session status values are `idle`, `generating`, `awaiting_tools` and `failed`. Queued work is shown in `queued_count`.
- `tool_call` SSE events are emitted after the assistant message and pending calls are committed, so a client can post results as soon as it sees the event.
- The extra user message for images in tool results is used for both providers, because neither documents image support in tool results.
- `GET /sessions` was added later (see DECISIONS.md). It runs one metadata query set per listed session (at most 500). If that becomes slow, fold `message_count`, `queued_count` and the active generation into one grouped query. The `sessions_owner_updated` index serves the ordering. Titles are read with SQLite's `json_extract`, which needs SQLite 3.38 or newer (or the JSON1 extension).
- `pending_tool_calls` comes straight from the `pending_tool_calls` table, so it costs one extra query per metadata request.
