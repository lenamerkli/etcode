# Backend decisions (agreed)

These decisions supplement the original API/model/tool sketches in [PLAN.md](PLAN.md). See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for proposed contracts and implementation steps. Proposed details in that document are not yet agreed decisions.

## API and state
- Store sessions and messages in SQLite. Sessions are listable (see the follow-up decisions) but not deletable yet.
- A session is created with a model/provider combination and a websearch on/off setting. Add an endpoint to change a session's model. Model changes between vision-capable and non-vision-capable models are forbidden.
- `GET /session/<id>` returns metadata only. Add an endpoint for the entire message history. The last-N endpoint returns indexed entries from the full message list, including tool calls and tool results.
- Change message submission to support new messages, start generation immediately when the last message is not an assistant message, and respond without waiting for generation. Client tool results arrive through the message endpoint(s). New user messages received while generation is running are queued. History is append-only for now.
- Include `tool_call_id` in tool results to associate them with assistant tool calls. After an assistant tool call, wait for the client to supply results before continuing.
- Assistant messages have one `TextContent` for text and another for thinking/reasoning.
- SSE for generation streaming: text and reasoning deltas, complete tool calls, message completion, errors, cancellation, done. Connections during an active generation replay its already-produced events and continue live; if none is active, respond 204. Multiple listeners per session are allowed.
- Cancelling an active generation aborts upstream streaming, preserves the partial assistant message, appends a cancellation notice to that message, and sends a cancellation event. Provider failures are recorded in history. For now, fail instead of truncating/summarizing an overlong context.

## Providers, models and tools
- Support Arena API and OpenRouter. `/models` returns a curated hard-coded catalog, not live discovery. Each model has `name`, `tech_name`, `context_length`, `supports_images`, `provider`, `provider_tech_name` and provider-specific fields as needed (e.g. `openrouter_internal_provider`, nullable when not pinned). Model names in the question are examples of the desired format, not a confirmed exhaustive catalog.
- Image capability is catalogued per model. OpenRouter does not support images in tool results: adapt such responses into synthetic user messages for its upstream requests, rather than altering the client-visible conversation.
- Retry transient failures when likely to succeed, observing provider-supplied retry delays. Never retry an Arena 403.
- Expose temperature, max_tokens and reasoning effort to the client; parallel tool calls are always enabled.
- Keep tool schemas as Python dictionaries. Do not accept custom client tools. The backend advertises tools but does not execute client-side tools. Backend-owned tools (`websearch` and future ones) run on the backend when the client asks through `POST /session/<id>/tool-calls/<call_id>/run`; the client runs every call of a round serially in model order and the backend resumes once all results are stored.
- Upload and download images by content hash using additional endpoints. For client-executed `view_image`, the client uploads the image before returning its hash in a tool result. Use provider-compatible image encoding (usually base64 data URLs). Reject unsupported media; the initial Arena and OpenRouter integrations support only text and images.
- Use `filetype` or similar for detection. Choose sensible defaults under a configurable `data/` directory. Correct the example content-class bugs rather than copying them literally.

## Runtime and delivery
- Support environment variables and `.env` for secrets/configuration (including provider keys). Support multiple backend auth tokens. Add token authentication to backend endpoints.
- Use Flask with Gunicorn, Python 3.14 if feasible (otherwise 3.13), Pydantic for validation. Code goes in `etcode_backend/etcode_backend/`, with `requirements.txt` and tests. The backend runs independently from etcode_manager, may run on a host or server, and binds to `0.0.0.0` on an ordinarily unused port.
- Provide a default system prompt.

## Follow-up decisions
- Websearch uses Brave Search behind a pluggable search-provider interface, so the provider is easy to change.
- The model catalog is the curated 34-entry list in `catalog.py` (Arena Coding Router plus 33 OpenRouter models). Every entry must support native tool calling, which etcode requires; that is an entry requirement, not a stored capability flag. `tech_name` is the display-name slug for the Arena entry (`arena-coding-router`) and the part after `/` of the OpenRouter slug for OpenRouter entries, with `:free` variant suffixes kept (`nemotron-3-ultra-550b-a55b:free`). DeepSeek V4.1 Flash is pinned to Together via `openrouter_internal_provider`; the other OpenRouter entries route freely. No fallback catalog prices are set: OpenRouter reports `usage.cost` itself.
- Sessions (and uploaded media) are owned by the token that created them; other tokens cannot see them.
- Accepted: batch `POST /session/<id>/messages` with `{"messages": [...]}`; user messages arriving during a tool-call chain wait until the chain finishes; inference options are set per session and may be overridden per submission.
- Cost is accumulated per session in USD from the provider-reported `usage.cost`, with optional catalog prices as a fallback. Generations that produced output but cannot be priced are counted in `unpriced_generations` instead of being treated as free.
- `GET /sessions` lists the caller's own sessions (ownership as everywhere), most recently active first, with `limit`/`offset` paging (default 100, at most 500) and a `title` taken from the first user message. Sessions still cannot be deleted. Requested by etcode_desktop, which used to have to remember session ids itself.
- Session metadata includes `pending_tool_calls` (`id`, `name`, `arguments`, `resolved`, `backend`, in call order, without result content), so that a client can recover after a restart without scanning the history. Clients execute all calls serially in that order; backend-owned tools are invoked through `POST /session/<id>/tool-calls/<call_id>/run`.
- The `read_file` tool description and its parameter descriptions state the exact reading rules (start at the later of line and character start, stop at the first of the end limits, the defaults, what the result reports). Parameter names and defaults are unchanged and covered by a test.
