# etcode_desktop plan

`etcode_desktop` is the PySide6 GUI that runs **inside an etcode VM**. It talks to
`etcode_backend` (on the VM's host or on a server), shows the conversation, remembers
which sessions exist, and executes the tool calls the model makes against the VM.

Read together with `../etcode_backend/README.md` (API), `../etcode_backend/DECISIONS.md`
(agreed backend behaviour) and `../etcode_manager/PLAN.md` (how the VM is built and started).

## 0. Decisions already made

| # | Decision |
| --- | --- |
| 1 | **PySide6**. |
| 2 | The **manager provides the access token** (and backend URL) to the VM. The user can **change it in the GUI**. The GUI value wins over the provisioned one. |
| 3 | Tool calls **run automatically**. There is no approval mode. |
| 4 | `replace_in_file` **does not fail** on multiple matches. It replaces every occurrence and reports each one. |
| 5 | Tool calls from one response **run one at a time, in model order**. |
| 6 | The backend gets a **session list endpoint** and **pending tool calls in session metadata** (section 7). |
| 7 | The desktop app **autostarts in the guest**. The manager does not launch it. |
| 8 | Tool output formats follow the supplied examples (section 5.5). |
| 9 | `read_file` starts at the **later** of `start_line` and `start_char`, stops at whichever limit is reached **first** (`end_line`, `end_char`, or the `max_chars` cap), and reports what was read and what was left out (section 5.5). |
| 10 | `replace_in_file` with zero matches still prints the (empty) `<occurrences>` block, like the TypeScript. |
| 11 | Mixed client and backend tool calls share one order (section 5.5). |
| 12 | Session titles are renamed **locally only**. There is no `PATCH /session/<id>`. |
| 13 | In `read_file`, `end_char` is an **absolute, exclusive, 0-indexed offset**, like `start_char`. |
| 14 | The `read_file` tool descriptions in the backend are rewritten to state the rules above (section 7.3). |
| 15 | In `read_file`, `unread_chars` and `unread_lines` count **everything not returned**, both before the start and after the end. |

## 1. What the existing code constrains

| Fact | Consequence for the desktop client |
| --- | --- |
| Today the backend has **no "list sessions" endpoint**, and sessions can't be deleted. | Requirement 3 still holds: the client stores session ids itself. Once `GET /sessions` exists (section 7), the local store is merged with the server list. The client still works against a backend that lacks the endpoint. |
| Sessions are **owned by the bearer token**. Other tokens get 404. | Key local sessions by `(backend_url, token fingerprint)`. Never show sessions from another token or server. Changing the token in the GUI switches to that token's session list. |
| Manager starts QEMU with `-netdev user`. | The host is reachable from the guest at `10.0.2.2`. The default backend URL is `http://10.0.2.2:8765`. A server is just another URL. |
| Backend emits a `tool_call` SSE event for **every** call, including `websearch`, and marks each pending call with `backend`. It rejects client results for a backend tool with 409 `backend_tool`. | Client tools are `bash`, `read_file`, `write_to_file`, `replace_in_file`, `view_image`. Backend tools are run through `POST /session/<id>/tool-calls/<call_id>/run`. |
| The session only resumes once **all** client calls have a result. Results can arrive in any order and one by one. A repeated identical result is accepted as a duplicate. A different result for the same call is a 409. Results for calls that have already been resolved are a 409 `unknown_tool_call`. | Parallel execution fits well: post each result as soon as it exists. Journal what was posted (section 5.4) so a restart can resend the same bytes safely. |
| Tool-result `content` must be non-empty. Text is capped at `max_text_chars` (2 M). Uploads are capped at 20 MiB. | The executor never returns an empty result and truncates output. Errors are returned as text results, never as exceptions. |
| `POST /session/<id>/cancel` cancels a running generation. If none is running, it resolves outstanding tool calls as "cancelled". It returns 409 if there is nothing to cancel. | One "Stop" button covers both phases. Treat 409 as "already finished". |
| `tool_call` events are sent after the assistant message and pending calls are committed. `message_complete.message.tool_calls` holds the full list. | It's safe to post results as soon as a call arrives. Use `message_complete` as the source of truth for the call list. |
| SSE replays the active generation from the start on connect and supports `Last-Event-ID`. `done.next_generation_id` chains generations. 204 means nothing is active. | Reconnecting is cheap and idempotent. In-progress text comes only from the stream. Committed messages come from `/messages`. |
| Server keep-alive is a `: keepalive` comment every 15 s. | The client read timeout is about 45 s. A timeout triggers reconnect with `Last-Event-ID`. |
| The VM image has an **empty** venv at `/opt/etcode/.venv`, `agent` has passwordless sudo, and `/etc/xdg/autostart/` is already used for `etcode-no-blank.desktop`. | The desktop and its dependencies are installed by the image build and autostarted the same way. The model can read any file the app can read (see 8). |
| `seed.py` renders the cloud-init `user-data` and the user `agent` is created by `provision.sh` at image build time. | The manager can deliver the token with cloud-init `write_files` (section 8). |

## 2. Goals and non-goals

Goals (the four requirements):

1. **Run in the VM and talk to the backend** over HTTP and SSE with a bearer token. This works for a local host or a remote server.
2. **A GUI for the backend**: sessions, streaming chat, reasoning, tool-call cards, images, model and option controls, cost, stop.
3. **Store session ids** locally, persistently and per backend and token.
4. **Execute the tool calls** reliably, one at a time in model order (client and backend tools), and report the results back.

Non-goals for the first version: confirmation prompts for tool calls, sandboxing inside the VM, editing or deleting history (the backend is append-only), deleting sessions on the backend, a custom tool registry (the backend doesn't accept custom tools), audio or video.

## 3. Technology

* **PySide6** (>= 6.5; the dev `.venv` already has 6.12 essentials). `QtWidgets` is enough. This matches `etcode_manager`.
* **requests** for HTTP and SSE, which is already in the project venv. Use blocking calls on worker threads, not `QtNetwork`. Keeping the network layer free of Qt makes it testable without a display.
* **sqlite3** (stdlib) for the local store, **pytest** for tests, and no other runtime dependencies.
* Markdown is rendered by `QTextBrowser.setMarkdown()`. QtWebEngine isn't in the essentials wheel and adds hundreds of MB to the VM image.

## 4. Architecture

```
etcode_desktop/
  PLAN.md  README.md  requirements.txt  pyproject.toml
  etcode_desktop/
    __main__.py            entry: python -m etcode_desktop (single-instance guard)
    config.py              layered settings (env > GUI > provisioned > defaults)
    core/                  <- no Qt imports; unit-testable headlessly
      api.py               BackendClient (requests wrapper, typed errors)
      sse.py               SSE parser + resilient stream reader
      models.py            dataclasses for session meta, messages, events
      store.py             SessionStore (SQLite): sessions, journal, kv
      controller.py        SessionController: the turn state machine
      tools/
        __init__.py        registry, schema validation, dispatch
        scheduler.py       serial execution in model order
        bash.py  files.py  image.py  format.py
    ui/                    <- PySide6 only
      main_window.py       sidebar + chat + composer + status bar
      session_list.py  transcript.py  composer.py  tool_card.py
      settings_dialog.py   connection dialog (URL, token, reset to provisioned)
      bridge.py            QObject adapting controller callbacks to Signals
      markdown.py          markdown/HTML helpers, image cache
  packaging/
    etcode-desktop.desktop autostart + panel launcher entry
    etcode-desktop         launcher script
  tests/
    fake_backend.py        tiny Flask app that mimics the real API
    test_sse.py test_store.py test_controller.py test_tools_*.py test_config.py test_gui_smoke.py
```

### Threads

* **GUI thread**: widgets only.
* **One `SessionWorker` thread per active session** runs the controller loop: submit, read the SSE stream, hand tool calls to the tool scheduler, wait for them, follow the next generation. It talks to the GUI only via Qt signals through `ui/bridge.py`. A single loop per session means "what comes next" never races between threads. Different sessions run independently, so a background session keeps working while another is open.
* **Tool scheduler**: a small daemon-thread pool shared by the whole app. One pool job runs all calls of one assistant message serially, in model order. Section 5.5 describes the ordering rules.
* **Short-lived tasks** (`/models`, uploads, image downloads) run on a `QThreadPool` via a small `Task` helper.
* `requests.Session` is not thread-safe, so each thread gets its own session (`threading.local`). All SQLite access goes through a `SessionStore` that opens one connection per thread (WAL mode) and serialises writes.

## 5. Component design

### 5.1 BackendClient (`core/api.py`)

A thin wrapper with one method per endpoint: `models()`, `list_sessions()`, `create_session()`, `get_session()`, `set_model()`, `messages()`, `last()`, `submit()`, `cancel()`, `upload_image()`, `download_image()`, `stream()`.

* Sends `Authorization: Bearer <token>`. It never logs the token.
* Maps `{"error": {"code", "message"}}` bodies to `BackendError(status, code, message)`. Subclasses: `AuthError` (401), `NotFound` (404), `Conflict` (409), `Unreachable` (connection, DNS, timeout).
* The default timeout is 10 s connect and 30 s read. Streams and uploads use their own timeouts.
* Retries safe idempotent requests (GET, and `submit` of tool results because the backend dedupes them). **Never** auto-retry a user-message submit, since a retry can double-send. If the outcome is unknown, ask the backend (`GET /session`, message count) and let the user decide.
* `list_sessions()` raises `Unsupported` if the backend answers 404 or 405 for `GET /sessions`. The caller falls back to the local store only. `get_session()` tolerates a missing `pending_tool_calls` field for the same reason.

### 5.2 SSE reader (`core/sse.py`)

* A parser for `id:`, `event:`, `data:` and `:comment` lines, with multi-line `data` support. `data` is JSON. Unknown event kinds are ignored, so the client keeps working if the backend adds events.
* `follow(session_id, since=None)` yields typed events. It tracks the last `id`. On a network error or read timeout it reconnects with `Last-Event-ID` and exponential backoff (1 s up to 15 s). It gives up after about 2 minutes and surfaces `Unreachable`.
* 204 on connect means there is no active generation. The caller then re-reads the session status. The controller treats this as normal, not as an error (see 5.4, "after `done`").
* `Last-Event-ID` is only valid for the active generation (the backend returns 400 `invalid_event_id` otherwise). On that error, drop the id and reconnect from the start of the current generation.

### 5.3 SessionStore (`core/store.py`)

This is requirement 3. A SQLite file at `~/.local/share/etcode_desktop/store.sqlite3` (XDG data dir, mode 0600).

```sql
CREATE TABLE sessions (
  id            TEXT NOT NULL,
  backend_url   TEXT NOT NULL,
  token_fp      TEXT NOT NULL,        -- sha256(token)[:16], never the token
  title         TEXT NOT NULL,        -- last known title: the server's, or a local one. Empty until there is one
  title_custom  INTEGER NOT NULL,     -- 1: the user renamed the session, so the server's title is ignored
  updated_at    REAL NOT NULL,        -- last activity, as the backend reports it (the sidebar sorts by this)
  model         TEXT,                 -- last known
  status        TEXT,                 -- last known: idle/generating/awaiting_tools/failed/missing
  cost_total    TEXT,                 -- last known total_exact (decimal string)
  created_at    REAL NOT NULL,
  last_opened   REAL,
  last_seen_index INTEGER,            -- highest message index rendered
  archived      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (backend_url, token_fp, id)
);
CREATE TABLE tool_journal (           -- see 5.4
  backend_url TEXT, token_fp TEXT, session_id TEXT, call_id TEXT,
  name TEXT, arguments TEXT,
  state TEXT,                         -- 'running' | 'done' | 'posted'
  result TEXT,                        -- JSON content array exactly as it is posted
  started_at REAL, finished_at REAL,
  PRIMARY KEY (backend_url, token_fp, session_id, call_id)
);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);   -- last model, window geometry, UI prefs
```

Rules:
* The session id is written **immediately after `POST /session` returns 201**, before anything else. A crash right after creation therefore doesn't orphan it.
* **Merge with the server** on startup, on token or URL change, and every 30 s while the window is open. Sessions the server lists but the store doesn't know are imported (so sessions survive losing the local DB, and sessions made with another client show up). For sessions in both, the server's status, model and cost overwrite the cached values. Titles are kept locally if the user renamed a session, otherwise the server title is used. A local session that the server no longer lists and that returns 404 is marked `missing`. It stays visible and greyed out.
* "Remove" in the UI means *archive* locally. The backend can't delete sessions, so the label says "Remove from list". An archived session that the server still lists stays archived (the flag is local) and can be restored from a "Show archived" toggle.
* Secrets (the token) are never in this DB. Section 8 covers where the token lives.
* There's a "paste a session id" action to re-attach a session created elsewhere. With the list endpoint this is only needed for old backends.

### 5.4 SessionController and the turn state machine (`core/controller.py`)

The controller drives one session. It keeps no authority over history. The backend is the source of truth, and the controller only keeps what is needed to render and to execute tools.

```
            send(text/images)
 IDLE ─────────────────────────► SUBMITTING ──202──► STREAMING ◄──────────────┐
  ▲                                                      │                     │
  │ done(complete)                          message_complete has client calls  │
  │                                                      ▼                     │
  └────────────────────────────  (done, no tools) ◄── RUNNING_TOOLS ──all posted┘
                                    done(error/cancelled)  (calls run one at a time in model order; backend resumes
                                                            with a new generation after the last post)
```

**Sending**: upload attached images (`POST /media/images`), then `POST /session/<id>/message` with `content: [text, image...]` and current overrides. The response says `queued: true` if the session was busy. The UI shows a "queued" badge until the history index appears.

**Streaming**: connect to `/stream`. Events update the in-progress assistant message in memory: `reasoning_delta`, `text_delta`, `tool_call`, `usage`, `message_complete`, `error`, `cancelled`, `done`. `message_complete` carries the committed `index` and message. The controller replaces the live buffer with it and de-duplicates by index, so a replay can't draw a message twice.

**After `done`**:
* `next_generation_id` set: reconnect to `/stream` (this chains backend tools and queued messages).
* Status `tool_calls` and there are client calls: run them (below). When the last result is posted, the backend creates the next generation, and the controller reconnects.
* Otherwise, call `GET /session/<id>`. If `status` is `generating` or `awaiting_tools`, keep following. If `idle` or `failed`, go to IDLE. If a reconnect returns 204 right after posting results, retry up to about 3 times at 300 ms and then fall back to polling `GET /session` every 1 s. This covers the small window where the next generation isn't registered yet.

**Running tools (one at a time, in model order)**:
1. Take every unresolved call in `pending_tool_calls`, in `call_order` (or the stream/history order for an older backend).
2. Submit the batch to the tool scheduler, which runs the calls serially. For a client call: journal `running`, execute, journal `done`, `POST /message` for that result, journal `posted`. For a backend-owned call: `POST /session/<id>/tool-calls/<call_id>/run`; the backend stores the result. Move to the next call only after that step returned.
3. The executor always yields a result. Unknown tool, bad arguments, an exception or a timeout all become a text result. A failed post stops the rest of the batch; the journal resumes the round in order when the connection returns.
4. The loop waits until every call is resolved, then goes back to STREAMING. The backend resumes by itself.
5. UI cards show one `running…` card; the others say `waiting` until their turn.

**Crash and restart recovery** (`resume(session_id)`, called when a session is opened, and for every non-idle session when the app starts):
1. `GET /session/<id>` and `GET /session/<id>/messages`. Render the history, skipping the system message at index 0.
2. If `status == generating`, follow `/stream`.
3. If `status == awaiting_tools`, take `pending_tool_calls` from the session metadata (section 7). Each entry has `id`, `name`, `arguments`, `resolved` and `backend`. Walk the unresolved calls in order; for a client call, combine it with the journal:
   * `posted` or `done`: post the journaled result again. Duplicates are harmless and 409s are ignored.
   * `running` (the app died while executing): **do not re-run**. Re-running a shell command is not safe. Post `Error: the desktop client was interrupted while executing this tool call; the outcome is unknown. Check the current state before retrying.` The model can then inspect the state itself.
   * No journal row: execute it normally, in model order with the other missing calls.
   
   On a backend without `pending_tool_calls`, fall back to the last assistant message in the history and the calls that have no committed tool message. In that case calls that were accepted but not yet committed are resent from the journal.
4. `idle` or `failed`: show the input box (a failed session accepts a new user message).

**Stop**: set a cancel flag shared by all running tools of the session. Each `bash` kills its process group (SIGTERM, then SIGKILL after 2 s). Then call `POST /cancel`. Whichever phase it was in, the result is consistent: a generation is aborted with a partial message kept, or outstanding calls are resolved as cancelled. A 409 means "already finished" and is not shown as an error. A call that finishes after cancel gets a 409 on post, which is ignored.

**Model/option changes**: `PATCH /session/<id>/model` only while idle or failed, and only to a model with the same `supports_images`. The model picker greys out other entries and shows why. Temperature, max tokens and reasoning effort are sent as overrides on each submit. They are also chosen at session creation.

**Errors**: an `error` event ends up as an assistant message with `status: error`. It's shown in red with the code and message, and with a "retryable" hint. History is append-only and there is no retry endpoint, so the way forward is sending a new message. The composer offers a "Continue" shortcut that submits that text.

### 5.5 Tool executor (`core/tools/`)

The schemas are defined in `etcode_backend/tools.py`. The desktop **mirrors the same names and parameters** and validates each call's arguments against the same JSON schema (a copy kept in `core/tools/schemas.py`, with a test that fails if it drifts from the backend's definitions when both packages are present in the repo). Invalid arguments produce an error text result, never an exception.

All text results use the formats below. Python code builds them with plain string concatenation, so they match byte for byte. Paths in results are **echoed as the model gave them**, not the resolved path.

#### `bash(command, timeout=60, directory=/home/agent, venv?, max_chars=100000)`

```
<returncode>{code}</returncode>
<stderr>{stderr}</stderr>
<stdout>{stdout}</stdout>
```
(with a newline after `</returncode>` and after `</stderr>`; no trailing newline)

* Run `bash -c <command>` with `cwd=directory`, no stdin, `start_new_session=True`. `stdout` and `stderr` are captured separately by two reader threads.
* `venv` is applied by setting `VIRTUAL_ENV` and prepending `<venv>/bin` to `PATH` (and removing `PYTHONHOME`). The venv isn't activated by sourcing a script.
* The whole formatted string is truncated to `max_chars` (as in the supplied `truncate(output, maxChars)`), and a short `[output truncated, N more characters]` marker is appended. Each stream buffer is capped at `max_chars` while reading, so a runaway `yes` can't fill memory. The rest of that stream is drained and counted.
* On timeout: kill the process group, keep the partial output, and append `Command timed out after {timeout} seconds` to `stderr`. The return code is the real one (a negative signal number).
* If the process can't start (for example, a missing directory): return code `-1` and the error message in `stderr`, in the same format.

#### `read_file(path, start_line=1, end_line=1000, max_chars=1000000, start_char=0, end_char=100000)`

```
<path>{path}</path>
<first_char>{first_char}</first_char>
<last_char>{last_char}</last_char>
<first_line>{first_line}</first_line>
<last_line>{last_line}</last_line>
<total_lines>{total_lines}</total_lines>
<unread_chars>{unread_chars}</unread_chars>
<unread_lines>{unread_lines}</unread_lines>
<content>{text}</content>
```
(newline between elements, no trailing newline)

Rules (decision 9). The earlier snippet was only a rough sketch, so these rules replace it:

1. **Start** at `start_line` or `start_char`, **whichever comes later** in the file.
2. **Stop** when the first of these limits is reached: the end of line `end_line`, the character position `end_char`, or `max_chars` characters read.
3. **Report** which part of the file was read, and how much was left out.

Reference algorithm (Python):

```python
content = read_text(path)                      # UTF-8, errors="replace", newline=""
total_chars = len(content)
lines = content.splitlines(keepends=True)
line_starts = []                               # character offset where line i + 1 begins
cursor = 0
for line in lines:
    line_starts.append(cursor)
    cursor += len(line)

# 1. start: the later of the two
line_start = line_starts[start_line - 1] if 1 <= start_line <= len(lines) else (total_chars if start_line > len(lines) else 0)
start = min(max(line_start, start_char, 0), total_chars)

# 2. stop: the earliest of the limits
line_end = line_starts[end_line] if 1 <= end_line < len(lines) else (total_chars if end_line >= 1 else 0)   # end of line `end_line`
end = max(start, min(line_end, end_char, start + max_chars, total_chars))

text = content[start:end]
```

* **Defaults always apply.** An omitted parameter takes its schema default. So a bare call reads from the start of the file and stops at the first of: the end of line 1000, character 100000, or 1000000 characters read. To read a file that continues past line 1000 or character 100000, the model sets `end_line` and `end_char` explicitly.
* **`end_line` is inclusive** (line `end_line` is the last line read), and lines are 1-indexed. **`end_char` is an exclusive, absolute, 0-indexed offset**, like `start_char`. **`max_chars` is a count of characters** read from the start position.
* **Text and lines.** `content` is read with `newline=""`, so `\r\n` is **not** translated and the offsets match the real file (and `replace_in_file`). Lines are `str.splitlines(keepends=True)`. That also splits on `\r`, `\v`, `\f`, `\x1c`–`\x1e`, `\x85`, `\u2028` and `\u2029`. "Characters" are Python code points.
* **The metadata describes what was really returned**, not what was requested. It's computed from the final `start` and `end`:
  * `first_char = start` and `last_char = end`. `last_char` is an exclusive end, so the next page after a cut-off read is `start_char=last_char` (together with `end_char` raised past it).
  * `total_lines = len(lines)` (0 for an empty file).
  * `first_line` = the 1-indexed line containing `start` (clamped to the last line), or `0` for an empty file. `last_line` = the line containing `end - 1`, or empty (`<last_line></last_line>`) if nothing was read.
  * **Left out**: `unread_chars = total_chars - len(text)` and `unread_lines = total_lines - (last_line - first_line + 1)`, or `total_lines` if nothing was read. Both count everything that was **not** returned, before the start and after the end. A line that was read only in part counts as read. The size of each side follows from the other fields: characters before the range are `first_char`, and characters after it are `total_chars - last_char`.
* Reading a position at or past the end of the file is not an error. It returns empty content, with `first_char = last_char = total_chars` and everything counted as unread.
* Lookups of a line from an offset use `bisect` on `line_starts`.
* Errors (missing file, directory, binary content that isn't text) return a plain `Error: ...` text.

Examples for the tests, with `content = "a\nbb\nccc\n"` (9 characters, 3 lines):

| Arguments | first_char | last_char | first_line | last_line | unread_chars | unread_lines |
| --- | --- | --- | --- | --- | --- | --- |
| *(defaults)* | 0 | 9 | 1 | 3 | 0 | 0 |
| `end_line=2` | 0 | 5 | 1 | 2 | 4 | 1 |
| `start_line=2, end_line=2` | 2 | 5 | 2 | 2 | 6 | 2 |
| `start_line=3, start_char=0` (the later start wins) | 5 | 9 | 3 | 3 | 5 | 2 |
| `start_char=4, end_char=6` (ends inside line 3) | 4 | 6 | 2 | 3 | 7 | 1 |
| `end_char=3` (ends inside line 2) | 0 | 3 | 1 | 2 | 6 | 1 |
| `max_chars=4` | 0 | 4 | 1 | 2 | 5 | 1 |
| `start_char=100` | 9 | 9 | 3 | *(empty)* | 9 | 3 |

#### `write_to_file(path, content)`

`Wrote {characters} characters to {path}`

Create parent directories, write atomically (temp file in the same directory, then rename), and keep the mode of an existing file. `characters` is `len(content)`. Text is written as UTF-8 without newline translation.

#### `replace_in_file(path, search, replace, read=false)`

```
<note>Made {replacements} replacement(s) in <path>{path}</path></note>
<occurrences>
<occurrence>
<old_start_character>{old_start}</old_start_character>
<old_end_character>{old_end}</old_end_character>
<new_start_character>{new_start}</new_start_character>
<new_end_character>{new_end}</new_end_character>
</occurrence>
</occurrences>
<content>{whole file after replacement}</content>
```
(every element, including the last one, is followed by a newline, exactly like the supplied snippet)

* Exact, non-regex matching. Scan **left to right, non-overlapping**, and **replace every occurrence** (decision 4). More than one match is not an error.
* `old_start`/`old_end` are offsets in the original text. `new_start`/`new_end` are offsets in the new text. Starts are 0-indexed and ends are **exclusive** (same convention as `last_char` in `read_file`). The new offsets account for the length change of all earlier replacements. Offsets count Python code points.
* The `<occurrences>` block is **always** printed (decision 10), with one `<occurrence>` per replacement. With zero matches the result is the `<note>` (`Made 0 replacement(s)`), then an empty `<occurrences>\n</occurrences>\n`, and the file is not touched.
* The `<content>` block is only written when `read` is true. It contains the entire file after the replacement (or unchanged, when nothing matched).
* An empty `search` is the only validation error (`Error: search must not be empty`), because it would match everywhere. A missing file or non-text content is also reported as `Error: ...`.
* Write atomically and keep the file mode. The file is read and written with `newline=""`, so line endings are untouched.

#### `view_image(path)`

Check the magic bytes (png/jpeg/webp), check the size against the 20 MiB limit, upload via `POST /media/images`, and return `[{"type":"image","hash":...}, {"type":"text","text":"Image {path} ({mime}, {size} bytes)"}]`. The backend detail is that it forwards the image upstream in an extra user message, so the result needs no further handling.

#### Serial execution rules (`core/tools/scheduler.py`)

* All calls of an assistant message run **one after another, in the order the model gave them**. One pool job owns the whole round; the next call starts only after the previous call's result was journaled and accepted by the backend.
* Client tools (`bash`, files, `view_image`) run in the VM. Backend-owned tools (`websearch` and future ones) are executed by the backend through `POST /session/<id>/tool-calls/<call_id>/run`. Both kinds share the same order.
* A failure to post a result stops the rest of the round. The follower retries; the journal resends the failed result first, then the calls that never ran.
* Stop cancels the current call and skips the queued ones. The backend resolves them as cancelled.
* `max_parallel_tools` is deprecated and ignored. The pool only bounds how many sessions may run a tool round at the same time.
* File writes still use the atomic rename above.

## 6. GUI

Layout (single window, about 1100x750, remembers geometry, opens maximized on autostart):

```
┌────────────┬───────────────────────────────────────────────┐
│ [+ New]    │  transcript (scrolling)                       │
│ Sessions   │   user bubble                                 │
│  • title ● │   ▸ Thinking… (collapsible)                   │
│  • title   │   assistant markdown                          │
│  • title ⚠ │   ┌ tool: bash  ─ `ls -la` ─ ✔ 0.4s ┐ expand  │
│            │   ┌ tool: read_file ─ a.py ─ ⟳      ┐         │
│            ├───────────────────────────────────────────────┤
│            │ [model ▾] [web ☐] [effort ▾] [⚙]              │
│            │ composer (Enter=send, Shift+Enter=newline)    │
│            │ [📎 image]                    [Stop] [Send]   │
├────────────┴───────────────────────────────────────────────┤
│ ● connected http://10.0.2.2:8765 │ session: generating │ $0.0123 │
└────────────────────────────────────────────────────────────┘
```

* **Sidebar**: sessions sorted by last activity (the server's `updated_at`). A status dot (idle, generating, awaiting_tools, failed, missing) comes from the merged list (section 5.3), and no per-session polling is needed when the backend supports `GET /sessions`. Context menu: rename, copy id, remove from list. Without the list endpoint, the client falls back to `GET /session/<id>` per visible session every 30 s, skipping sessions the controller is already following.
* **Transcript**: one widget per message, not one giant document. This keeps streaming updates cheap. Assistant text is rendered with `setMarkdown`. Reasoning is a collapsed block, streaming live. Tool calls are cards: header (tool name, short argument summary, spinner or exit status and duration) and an expandable body (arguments and output in monospace, truncated in the UI, with the full text on "Show all"). Tool results are attached to their card, matched by `tool_call_id`, and are not shown as separate messages. Images are shown as thumbnails (click to enlarge). Messages loaded from history use the same renderer as live ones.
* **Streaming performance**: buffer deltas and flush to the widget on a 50 ms `QTimer`. Re-render markdown only for the message being streamed. Auto-scroll only if the user is already at the bottom, otherwise show a "jump to latest" button.
* **Composer**: multi-line, Enter to send, Shift+Enter for a new line. Paste or drag-and-drop images (PNG/JPEG/WebP/GIF, with a size check before upload). While the session is busy, the Send button changes to "Queue", matching the backend's queued-message semantics.
* **Connection settings dialog** (menu and the gear button): backend URL, token (password field), "Test connection" (calls `GET /models`), and a "Reset to provisioned values" button that deletes the GUI overrides. It shows where each value currently comes from ("provisioned by manager" or "set in the GUI"). `GET /models` also fills the model picker. Models with `available: false` are shown disabled with a tooltip. If no token exists at all (a VM created before the manager provided one), the dialog opens on startup.
* **Status bar**: connection state, session status, and the session cost from `cost.total_exact`. Show "≥ $x (n unpriced)" if `unpriced_generations > 0`, since the total is then only a lower bound.
* **Keyboard**: Ctrl+N new session, Ctrl+Enter send, Esc stop, Ctrl+K focus the session list.
* **Errors in the UI**: a dismissible banner for connection problems with an automatic retry countdown, and a prompt for 401 that opens the connection dialog. Backend error messages are shown verbatim, never raw tracebacks.
* **Single instance**: a `QLockFile` plus `QLocalServer` ensures that autostart and a manual launch don't create two windows. A second launch raises the existing window.

## 7. Backend changes (`etcode_backend`)

Decision 6 adds two features to the backend, and decision 14 changes one tool description. They're small and backward compatible. They go in before milestone 3 of the desktop work (section 10), and the desktop still works without them.

### 7.1 `GET /sessions`

* Auth and ownership as everywhere: only the caller's sessions (`WHERE owner=?`, already indexed by `sessions_owner`).
* Query parameters: `limit` (default 100, max 500), `offset` (default 0). Sorted by `updated_at` descending, with `id` as the tie-breaker.
* Response: `{"sessions": [<session metadata>...], "total": n}`. Each entry is the same shape as `GET /session/<id>` (reusing `session_metadata` and `_metadata`), plus a `title` field: the first 80 characters of the first user text message, or `null` if there is none yet.
* `message_count`, `queued_count` and the active generation use the existing per-session queries. At 100 rows this is fine for SQLite. If it becomes slow, move them into one grouped query.
* Sessions stay non-deletable. `DECISIONS.md` says "Sessions are not listable or deletable yet", so that file is updated to "listable, not deletable yet".

### 7.2 `pending_tool_calls` in session metadata

* `session_metadata` (so also `GET /session/<id>`, `GET /sessions` and the submit response) gets `pending_tool_calls: [{"id", "name", "arguments", "resolved"}]`, ordered by `call_order`. `resolved` is true when a result has been stored but the barrier hasn't cleared yet (the data is in `pending_tool_calls.result`). The list is empty when nothing is pending. Result content is not included.
* Backend-run calls appear in the list with `backend: true`. The client runs them by calling the backend tool endpoint in the same serial order.
* This lets the client recover after a restart without scanning history, and know which calls are already accepted.

### 7.3 `read_file` tool descriptions (decision 14)

The backend schema in `etcode_backend/tools.py` gets descriptions that state the exact rules, so the model uses `start_*`, `end_*` and `max_chars` correctly. The `default` values and parameter names don't change. The proposed texts:

| Field | Description |
| --- | --- |
| tool | `Read the contents of a file. Reading starts at start_line or start_char, whichever comes later, and stops at whichever of end_line, end_char or max_chars is reached first. Parameters you omit use their defaults, so a call with only a path returns at most the first 1000 lines and the first 100000 characters; to read more of a large file, set end_line and end_char explicitly. The result reports which part of the file was read (first_char, last_char, first_line, last_line) and how much was left out, before and after that part (unread_chars, unread_lines).` |
| `path` | `The path to the file to read` |
| `start_line` | `The line to start reading from, 1-indexed. If start_char is later in the file, start_char is used instead.` |
| `end_line` | `The last line to read, inclusive and 1-indexed. Reading stops at the end of this line or at end_char, whichever comes first.` |
| `start_char` | `The character offset to start reading from, 0-indexed. If start_line begins later in the file, start_line is used instead.` |
| `end_char` | `The character offset at which reading stops: an absolute, 0-indexed position in the file, exclusive. Reading stops here or at the end of end_line, whichever comes first. The default of 100000 means that a read starting after character 100000 returns nothing unless this is raised.` |
| `max_chars` | `The maximum number of characters to read, counted from the start position.` |

* The tool description has to be kept in step with the result format in section 5.5. It names the reported fields. The result format itself defines them: `last_char` is an exclusive end (so the next page is `start_char=last_char`), and `unread_*` count everything not returned (decision 15).
* The change goes into `etcode_backend/tools.py` together with the same text in the desktop's schema copy (`core/tools/schemas.py`). The drift test compares the two, so they can't diverge.
* No other tool descriptions change. The `bash` description keeps the existing text.

### 7.4 Work items


* Implementation in `sessions.py` and `api.py` (about 40 lines).
* Tests in `tests/test_api.py`: ownership isolation, ordering, pagination bounds, titles, `pending_tool_calls` across the whole tool round trip, and the empty list after cancel.
* Update the `read_file` descriptions in `tools.py` (7.3). Add a test that checks that the backend's advertised `read_file` schema keeps its parameter names and defaults.
* Documentation: `README.md` API table, `DECISIONS.md`, and the "Implementation notes" section of `IMPLEMENTATION_PLAN.md`.

## 8. Configuration, token and delivery

### 8.1 Settings layers

The desktop reads settings in this order (highest priority first):

1. Environment: `ETCODE_BACKEND_URL`, `ETCODE_API_TOKEN` (mainly for development and tests).
2. **GUI overrides**: `~/.config/etcode_desktop/config.json` (mode 0600, directory owned by `agent`), written by the connection dialog.
3. **Provisioned values**: `/etc/etcode/desktop.json`, written by the manager through cloud-init (below). The app only reads it.
4. Defaults: `http://10.0.2.2:8765`, no token.

Each key (`backend_url`, `token`) is resolved separately, so overriding only the URL keeps the provisioned token. "Reset to provisioned values" deletes the keys from layer 2.

### 8.2 Manager changes (`etcode_manager`)

* **Manager config** (`config.json`) gets `backend_url` and `backend_token`. The create-VM dialog shows both, prefilled with these defaults, so one VM can get its own token. They are stored in that VM's `vm.json` (mode 0600), because `seed.py` needs them whenever the seed is regenerated.
* **`seed.py`** adds a `write_files` section to `user-data`:
  ```yaml
  write_files:
    - path: /etc/etcode/desktop.json
      owner: agent:agent
      permissions: "0600"
      defer: true
      content: |
        {"backend_url": "...", "token": "..."}
  ```
  The content is built with `json.dumps` and then indented into the YAML block, so quotes and special characters in tokens can't break the file. `defer: true` runs the write late in boot, after the `agent` user is certain to exist. `/etc/etcode/` is created by cloud-init as root, and the file is owned by `agent` so the app can read it. The app never writes into `/etc/etcode/`, which is why GUI changes go to the separate user config.
* **Limitation**: cloud-init runs `write_files` once per instance id, and the manager keeps the instance id stable. If the user changes the token in the manager for an existing VM, the VM does not pick it up. They change it in the GUI instead. This is exactly why the GUI override exists. (The manager could later offer "apply to running VM" over SSH. That is out of scope.) This is to be verified on a real VM (milestone 8).
* The token ends up in the seed ISO on the host disk next to the VM, and in `vm.json`. Both are on the user's own machine. Mode 0600 on `vm.json`, and the README of the manager mentions it. **Never bake a token into the template image.**

### 8.3 Image changes (`build_image.sh`, `provision.sh`)

* Install the app into `/opt/etcode/etcode_desktop` and its dependencies (PySide6, requests) into the existing `/opt/etcode/.venv`. Add the launcher `/usr/local/bin/etcode-desktop`.
* **Autostart** (decision 7): install `etcode-desktop.desktop` into `/etc/xdg/autostart/`, the same mechanism as `etcode-no-blank.desktop`. Also add a panel launcher entry (like the existing `panel/launcher-*`) so the app can be reopened if it was closed. Use `X-GNOME-Autostart-Delay=2` so the panel is up first.
* Qt needs system libraries on Debian (`libegl1`, `libxkbcommon-x11-0`, `libxcb-cursor0`, `libxcb-icccm4`, `libxcb-image0`, `libxcb-keysyms1`, `libxcb-randr0`, `libxcb-render-util0`, `libxcb-shape0`, `libxcb-xinerama0`, `libgl1`). The exact list is verified in the image. `QT_QPA_PLATFORM=xcb`. Alternative: Debian's `python3-pyside6.*` packages, which are less flexible and may be older.
* The default system prompt in the backend already tells the model where skills and scripts are. No change needed.

### 8.4 Where session ids live

The local store is in the VM's overlay disk. With `GET /sessions` the sessions are recoverable from the server even if the VM is deleted, as long as the **same token** is used for a new VM. That is another reason to give each VM its own token only when isolation matters more than recoverability.

## 9. Security notes

* The agent (the model) runs commands as `agent` with passwordless sudo, so it can read `/etc/etcode/desktop.json`, `~/.config/etcode_desktop/config.json`, the store and the process environment. **Assume the model can see the backend token.** That is acceptable because backend tokens only reach their own owner's sessions, so the damage is limited to those sessions and some API spend. Use a token dedicated to etcode VMs, not one used for anything else. Use one per VM when sessions must be isolated.
* Use HTTPS in front of any non-local backend. Warn in the connection dialog if the URL is `http://` and not a private or loopback address.
* Tool calls run automatically (decision 3) and without path restrictions. The VM is the security boundary, not the app.
* Never log tokens, authorization headers or full tool output. Logs go to `~/.local/state/etcode_desktop/desktop.log` with rotation. Tool output in logs is limited to the first few hundred characters.
* Markdown from the model is untrusted. Disable external resource loading in the `QTextBrowser` (`setOpenLinks(False)`, links open only after confirmation, and no remote images).

## 10. Testing

Keep the protocol layer free of Qt and test it headlessly, like the manager.

* **`fake_backend.py`**: a small Flask app (the backend's own dependency) that implements the endpoints used by the client, including `GET /sessions` and `pending_tool_calls`, and can be scripted with canned SSE sequences: text only, text and tool call, several calls in one message, mixed websearch and bash calls, error mid-stream, cancellation, drops (to exercise `Last-Event-ID`), a 204 right after submit, and 409 variants. It can also run in a "legacy" mode without the two new features. If it is easy, also run a smoke test against the **real** backend with a fake upstream, reusing `etcode_backend/tests/conftest.py`.
* **Unit tests**:
  * SSE parser (split chunks, multi-line data, comments, CRLF).
  * Config layering (env > GUI > provisioned > default, per key, reset).
  * Store (create, reopen, token isolation, archive, merge with the server list, `missing`).
  * Tool formats compared **byte for byte** with the examples in section 5.5. `bash` also covers timeouts, truncation, the process-group kill, `venv` and separate stdout/stderr. `read_file` covers the table of examples in section 5.5, the later-start and earliest-stop rules, paging with `last_char`, an empty file, a start past the end, files without a final newline, CRLF, multi-byte characters, and the metadata of empty reads. `replace_in_file` covers zero, one and many matches, overlapping patterns, growing and shrinking replacements (the new offsets), `read=true`, and an empty search. Also `write_to_file` atomicity and `view_image` validation.
  * Scheduler (calls run one after another in model order, bounded workers, stop skips queued calls, a failed post stops the rest).
  * Controller state machine (ordering, dedup by index, journal and crash recovery with several calls, 409 handling, stop in both phases, legacy backend fallback).
* **GUI smoke test** with `QT_QPA_PLATFORM=offscreen`: start the window against the fake backend, send a message, see streamed text, tool cards and a final state. Skipped if PySide6 is missing.
* **VM check** (manual, in the last milestone): create a VM from the manager, confirm the app autostarts with the provisioned token, change the token in the GUI and restart, then run a real session with `bash`, several calls in one message and `view_image`.

## 11. Milestones

1. **Skeleton and config.** Package layout, layered `config.py`, `requirements.txt`, logging, `python -m etcode_desktop` opening an empty window with the single-instance guard. **Done.** `requests` is not in `requirements.txt` yet. It is added in milestone 2, when it is first used.
2. **BackendClient + SSE + fake backend.** Typed errors, streaming with reconnect, tests. **Done.** Differences from the sketch in section 5.1/5.2: `submit()` always uses the batch route
(`POST /session/<id>/messages`), which also takes a single message. The event follower follows **one**
generation. Chaining through `done.next_generation_id` is left to the controller (milestone 5). Closing a
follower shuts the socket down, because closing it alone does not wake a thread blocked in `recv`. The fake
backend keeps a finished generation available to the stream endpoint (`linger`) so that replays can be
tested, and `test_contract_real_backend.py` runs the client against the real backend and worker.
3. **Backend additions** (section 7): `GET /sessions` and `pending_tool_calls`, with tests and docs. Can run in parallel with milestone 2. **Done.** Besides the two features, the `read_file` descriptions (7.3) are in `tools.py` word for word, with a test that pins the parameter names, types and defaults. Paging values outside 1 to 500 (`limit`) or 0 to 1000000 (`offset`) give 400 `invalid_number`, like `last/<n>`. The drift test against the desktop's own schema copy comes with the tool executor (milestone 6), because that copy does not exist yet.
4. **SessionStore.** Schema, per-`(url, token)` isolation, create and reopen, merge with the server list, tests. Wire "New session" and the sidebar. **Done.** The `tool_journal` table is not in the schema yet. It is added as migration 2 together with the tool executor (milestone 6), because the store has a schema version (`PRAGMA user_version`). The two extra columns above (`title_custom`, `updated_at`) were needed for the rename rule and the sort order. `core/sync.py` holds the merge logic (Qt-free), and `ui/bridge.py` runs it on daemon threads. The window also remembers its geometry and the "show removed sessions" choice in the store's `kv` table, and the **⋯** button next to "New session" holds "Add a session by its id…".
5. **Chat without tools.** Controller send and stream path, transcript widgets, markdown, reasoning, cost, stop (generation phase), queueing, history load. **Done.** How it is built: `core/controller.py` turns what the backend does into events, `core/session_state.py` and `core/transcript.py` turn the events into what the view shows (all Qt-free and tested without a display), and `ui/chat_session.py` hands the events from the worker threads to the GUI thread. After each generation the follower asks `GET /session/<id>` what comes next, instead of guessing from events, and fills gaps from the history (a stream that was over before it was opened, a queued message that was committed meanwhile). Messages that were sent but not yet committed are shown as `sending`, `queued`, `not sent` or `not confirmed`, and are matched with their committed copies by content. `EventFollower` got `on_interrupted`/`on_resumed` callbacks for the reconnect banner. **Tool calls** are shown as cards and the session waits for them (a notice explains this and Stop cancels them), so milestone 6 only has to run them. **Left for later milestones:** images in the composer and thumbnails in the conversation (milestone 8, until then an image shows as `[image abcd1234…]`), the model and option controls (8), the "Continue" shortcut after a failed answer (8), and following changes that another client makes to an idle session that is open (not planned). Bugs that the tests found on the way: a crash when a second launch had raised the first window (a lambda held the server side socket), long tool headers and unbroken words widening the conversation.
6. **Tool executor.** The five tools with the exact formats and tests, the serial scheduler, the journal, and the backend tool-call endpoint. Run them from the controller in model order, chain generations, stop (tools phase). **Done.** Where the plan said so: `core/tools/` has the five tools with the agreed formats (the examples in section 5.5 are tests, byte for byte), `scheduler.py` has the pool and the serial batch runner, the journal is store migration 2, and the controller runs, journals and posts the calls. The schema copy is `core/tools/schemas_data.py` (generated from the backend's definitions), `schemas.py` validates against it, and `tests/test_tool_schemas.py` fails when the two differ. Details that the plan left open: the default `directory` of `bash` (`/home/agent`) falls back to the home directory if it does not exist and the model did not name one; a `venv` without a `bin` directory, a `timeout` or `max_chars` below 1, and a path that is not text are errors for the model; the command's `ETCODE_API_TOKEN` is removed from its environment; a result is cut at 1 900 000 characters (the backend takes 2 000 000); finished journal entries are pruned after 7 days; a call that Stop ended is not posted (the backend resolves it, and a result could only start another generation); the controller does not ask the backend at the speed of the network when it keeps waiting for calls it has answered; and the app picks up sessions that were `generating` or `awaiting_tools` when it starts. The tool pool uses daemon threads, so that quitting never waits for a tool. Found by the tests on the way: a deadlock in the scheduler (a done-callback that took a lock the caller held), that tight request loop, a test that killed unrelated processes with `pkill -f`, and a fake backend whose open connections survived `stop()`.
7. **Recovery and robustness.** `resume()` for generating and awaiting_tools sessions (with and without `pending_tool_calls`), reconnect, 204 race, 401 and offline handling. **Done.** Much of this came with milestones 5 and 6 (`resume`, reconnecting with `Last-Event-ID`, the 204 race, the legacy backend path), and this milestone tested it against outages (the fake backend can now `go_down()` and `come_back()` on the same port, and the real backend's API can be restarted under a running generation) and filled the gaps: `retry_now()` on the follower and the controller ends a wait between attempts, `recover()` makes a conversation that was stuck try again when the bridge sees the backend answer (new `reconnected` signal), the bridge refreshes every 5 s while offline, the status bar has a countdown and a **Retry now** button, a message with an unknown outcome is resolved from the history after a grace period (new `UnknownResolved` event, buttons **Send again** and **Remove** on messages that surely did not arrive), `TranscriptModel.load` keeps messages that are not on the backend, a 401 while sending or verifying marks the session unavailable, the follower survives unexpected errors (three in a row end it with a message), the tool journal stopped depending on the token (store migration 3) and a journal that cannot be written means the command is not run, and SIGTERM, SIGINT and SIGHUP quit the app and end the model's commands. Worker threads no longer emit Qt signals into widgets (a deletion that races with an emit can crash the process): they post to `ui/dispatch.py`, a queue that a timer on the GUI thread drains. Found on the way: the intermittent segfaults of the full test run followed a failing GUI test (a test that raised kept windows alive until the garbage collector deleted them at a bad moment), and that was the reason for the dispatcher. **Not done here:** the 401 prompt that *opens the connection dialog*. The dialog itself comes with milestone 8, so until then a refused token shows "token rejected" and **Retry now**.
8. **Polish.** Image paste and attach, image cache, model and option controls, connection dialog, keyboard shortcuts, README. **Done.** Connection dialog (`ui/connection_dialog.py`, with `ConfigStore.provisioned_values()`, `override_values()` and `env_keys()`, and `is_insecure_url`), opened from the status bar, the menu, at startup without a token (`MainWindow.prompt_for_missing_token`) and when the backend refuses the token. Model picker, effort and options (`core/options.py`, `ui/model_bar.py`, `ui/options_dialog.py`): the model is changed with `PATCH /session/<id>/model` (`SessionController.change_model`), and the options go with every message as overrides and are remembered per session in the store's `kv`. The new-session dialog has the options, too. Pictures: `core/attachments.py` (checks by content and size), the composer (attach, paste, drop, chips), `SessionController.send` uploads attachments first (`ImagesUploaded` event) and a failed upload is a known failure that can be sent again, `ui/image_loader.py` (download once, private disk cache with pruning, in-memory thumbnails, failures remembered for 20 s, the pictures the app uploaded are never downloaded), thumbnails in user messages and tool cards, click to enlarge. **Continue** after an answer that ended in an error. Menus (Session, View, Help) with the shortcuts of the plan plus Ctrl+L, Ctrl+, and Ctrl+End. The window title names the session. The model catalog is fetched as soon as the backend answers (once). Differences from the sketch: the connection dialog's gear is in the status bar (the plan drew no spot for it), the model picker has the web search checkbox only as an indicator (the backend cannot change it), and the fake backend now refuses a model change between models with and without pictures like the real one does, and has a third model (`fake-coder-lite`).
9. **Manager and image integration.** `seed.py` `write_files`, manager config and create-VM fields, provisioning, system libraries, autostart entry and panel launcher, manual VM verification. **Done, except the manual check on a real VM** (see below). What was built: `etcode_manager/config.py` has `backend_url` and `backend_token` (validated with the desktop app's own rules, and a test compares the two), the *New VM* dialog has both fields (prefilled, the token hidden unless "Show the token" is checked, invalid values keep the dialog open), and `VM` stores them in `vm.json`, which is now written atomically with mode 0600 (`seed.iso` is 0600 too). `seed.py` builds `user-data` by concatenation (not `str.format`, since a token may hold braces) and adds the `write_files` entry from section 8.2 (one line of `json.dumps`, so any token is valid YAML; tested with PyYAML for tokens full of quotes, braces and non-ASCII). Keys without a value are left out. `packaging/` has the launcher `etcode-desktop`, `etcode-desktop.desktop` (menu and panel), `etcode-desktop-autostart.desktop` (`X-GNOME-Autostart-Delay=2`, `--maximized`) and an SVG icon. `build_image.sh` stages the app (without tests and caches), the launcher, the entries and the icon (`DESKTOP_DIR`, default `../etcode_desktop`) and adds the Qt libraries to the guest packages. `provision.sh` installs `PySide6-Essentials` and `requests` into `/opt/etcode/.venv` (the launcher runs the app with `PYTHONPATH`, so the app itself is not pip-installed), checks `ldd` on `libqxcb.so` and an offscreen start, and installs the launcher, menu entry, icon and autostart entry. The panel layout has the quick-launch plugin 11 for the app. Differences from the plan: (a) **`--wait-for-provisioning`**: `defer: true` writes `/etc/etcode/desktop.json` in cloud-init's last stage, and the autostart (autologin, 2 s delay) can run before that on the first boot, so the app would start without a token and open the connection dialog. The autostart entry therefore runs `etcode-desktop --wait-for-provisioning`, which waits until the file exists or `/run/cloud-init/result.json` appears, for 60 s at most, and then starts anyway. (b) `PySide6-Essentials` instead of `PySide6` in the image (everything the app imports is in it, and the Addons package is large); `requirements.txt` still says `PySide6` for development. (c) The library list is the one that `ldd` reports for PySide6 6.12 on Debian 13 (`libxcb-util1` and `libxcb-xkb1` are needed and were not in the plan's list; `libxcb-xinerama0` is kept for older Qt). (d) The manager's panel XML had a `--` inside a comment, which is not well-formed XML; it was reworded so that any parser reads the file. (e) The icon and the second `.desktop` file (autostart) are new files in `packaging/`. **Verified without a VM:** the manager's tests (config, seed, `vm.json`, dialog, image files), the launcher's tests (arguments, platform, waiting rules), the staging function and the install part of `provision.sh` run in a Debian 13 container, the `ldd` check fails when a library is removed, and the installed launcher starts the app on the real `xcb` platform under Xvfb and shows "token provisioned by the manager". **Not verified (needs a build host and a VM):** the `virt-customize` build itself, that cloud-init writes the file with the right owner on a real first boot, that the Xfce autostart delay and the panel button work, and the limitation that a changed token in the manager does not reach a booted VM. The image has to be rebuilt (`IMAGE_VERSION=v5 ./build_image.sh`) and `base_image` pointed at it.

## 12. Remaining open questions

None. All questions raised so far are answered and recorded in section 0.
