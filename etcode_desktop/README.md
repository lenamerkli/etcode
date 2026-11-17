# etcode-desktop

The PySide6 GUI that runs **inside an etcode VM**. It talks to `etcode_backend`
(on the VM's host or on a server), shows the conversation, remembers the sessions,
and runs the tool calls the model makes. See [PLAN.md](PLAN.md) for the design and
the milestones.

**Status: milestone 9.** The app is complete, and the packaging for the VM image is done (see "In the VM image";
it has not been tried on a real VM yet). The app starts, reads its settings, logs, enforces a single
window, talks to the backend, keeps a list of your sessions, has a working conversation view,
runs the model's tools in the VM, and copes with trouble (see "Tools" and "When things go
wrong"). It has the polish of a finished window too: pictures can be attached and are shown as
thumbnails, the model and its options can be changed per session, the connection settings are
edited in a dialog, and there are menus and keyboard shortcuts. See "Using the window".

## Run

```bash
pip install -r requirements.txt     # PySide6 (needs libEGL etc. on Debian, see below)
python -m etcode_desktop            # add --maximized for autostart, --no-single-instance for development
```

Qt options such as `-platform offscreen` are passed through to Qt.

On a bare Debian install PySide6 needs system libraries. `libegl1`, `libgl1`,
`libxkbcommon0`, `libfontconfig1` and `libdbus-1-3` were enough for the offscreen
platform. A real X11 session needs more (the `libxcb-*` packages, see PLAN.md 8.3).

## In the VM image

`etcode_manager` builds the VM image and creates the VMs; this app is only installed and fed by it
(PLAN.md sections 8 and 9, and `../etcode_manager/README.md`).

| File | Installed as | |
| --- | --- | --- |
| `etcode_desktop/` | `/opt/etcode/etcode_desktop/` | the code. PySide6-Essentials and requests are in `/opt/etcode/.venv` |
| `packaging/etcode-desktop` | `/usr/local/bin/etcode-desktop` | launcher: venv python, `PYTHONPATH`, `QT_QPA_PLATFORM=xcb` |
| `packaging/etcode-desktop.desktop` | `/usr/share/applications/` and the panel (quick launch) | menu entry |
| `packaging/etcode-desktop-autostart.desktop` | `/etc/xdg/autostart/etcode-desktop.desktop` | starts the app maximized, 2 s after login |
| `packaging/etcode-desktop.svg` | `/usr/share/icons/hicolor/scalable/apps/` | icon |

The backend URL and token come from `/etc/etcode/desktop.json`, which the manager's cloud-init seed writes
(owner `agent`, mode 0600). The autostart entry runs `etcode-desktop --wait-for-provisioning`: on the first boot it
waits (60 s at most, `ETCODE_PROVISIONING_WAIT`) until that file exists or cloud-init has finished, so that the app
does not start without its token. Without that option the launcher starts the app at once. A change to the token
in the manager does not reach a VM that has already booted: change it in the connection dialog.


Open a session in the sidebar. Its history is loaded, and if the backend is working on it, the stream is
joined where it is. Sessions you opened keep working in the background while you look at another one.

* **Enter** sends, **Shift+Enter** adds a line, **Esc** or the Stop button stops the model. While the model
  works the button says **Queue**: the backend commits the message when the current work is done.
* The view follows the text as it streams, unless you scrolled up. A "Jump to latest" button appears then.
* A message that could not be sent stays in the conversation, marked "not sent". If the connection broke
  while sending, it is marked "not confirmed": it may have arrived, and if it did it turns up in the
  conversation by itself. A message is never sent twice automatically.
* If the connection to the backend drops during an answer, the app reconnects and continues where it
  was, without losing or repeating text. A banner shows what is going on.
* The text comes from a model, so it is treated as untrusted: raw HTML is not shown as HTML, nothing is
  loaded for it, and a link opens only after you confirm it.
* The cost of the session is shown below the conversation. A leading ≥ means that some answers could not
  be priced, so the real cost is higher.

## Tools

The model works in the VM through six tools. The app runs them **automatically** (there is no
confirmation step: the VM is the sandbox, and the user `agent` has passwordless sudo anyway).

| Tool | What it does |
| --- | --- |
| `bash` | Runs `bash -c <command>` in a directory, with a timeout (60 s by default). The result is the return code, stderr and stdout, cut to `max_chars`. |
| `interactive_terminal` | Keeps a PTY shell by terminal ID within one chat so the model can interact with TUIs using `type`, `press`, `wait` and `print` actions. A terminal ID cannot be reused after the desktop app exits; the local store remembers cleaned-up IDs and returns an error if one is requested again. |
| `read_file` | Reads part of a text file: from the later of `start_line` and `start_char`, up to the first of `end_line`, `end_char` and `max_chars`. The result says what was read and how much was left out. |
| `write_to_file` | Writes a whole file (atomically, keeping its mode) and creates missing directories. |
| `replace_in_file` | Replaces **every** occurrence of a text in a file and reports where each one was. |
| `view_image` | Uploads a PNG, JPEG or WebP image so that the model can see it. |

`websearch` is run by the backend, not in the VM: the client asks the backend to execute it through
`POST /session/<id>/tool-calls/<call_id>/run`. The exact result formats are in [PLAN.md](PLAN.md) (section 5.5).
Relative paths are relative to the home directory. `~` is the home directory.

* **One at a time, in model order.** The calls of one message run strictly one after another, in the
  order the model gave them. Client tools run in the VM; backend-owned tools (`websearch` and future
  ones) run on the backend through the tool-call endpoint. A call starts only after the previous
  result was journaled and accepted. So "write the file, run a command on it, then read it back" in
  one message works exactly as written. The model may still emit several independent calls in one
  response; only their execution is serialized.
* **Stop** cancels the current tool action and cancels the call at the backend. A running `bash` command and its
  process group are terminated (SIGTERM, then SIGKILL). An `interactive_terminal` shell is persistent by design;
  Stop interrupts the action sequence, while the shell remains available for later interaction (use `CTRL+C` to
  interrupt its foreground program). A stopped call sends no result.
* **A timeout** ends the command and everything it started and reports the output so far. A background
  process that was started on purpose (a server, say) keeps running when the command is over.
* **Restarts are safe.** Every call is written to a journal before it runs, and its result before it is
  sent. After a crash or a lost connection a finished result is sent again, and a call that was
  *running* when the app died is **not run again**: the model is told that the outcome is unknown and can look
  at the state itself. When the app starts, sessions that were waiting for tool results are picked up
  without being opened.
* **Terminal cleanup.** Live PTY processes are owned by the desktop process and are ended on shutdown (and the shell is signaled if the app crashes). For each chat, `store.sqlite3` retains the IDs of terminals that were cleaned up. Asking for one of those IDs returns an error instead of silently starting a replacement.
* **Errors are answers.** A bad call, a missing file or a failing tool becomes an `Error: ...` text for the model,
  because the session waits for a result for every call.
* The command does not get the app's `ETCODE_API_TOKEN`. But it runs as the same user, so it can read the
  token file. Use a token that is dedicated to etcode VMs.

## Using the window

* **Pictures.** Attach a picture with the **📎 Picture** button, paste one (Ctrl+V), or drop a file on the message
  box. PNG, JPEG, WebP and GIF up to 20 MiB, up to 10 per message, checked by their content (not their name)
  before anything is uploaded. Small chips show what will be sent, and each can be removed. The button is off when the
  session's model cannot look at pictures. Pictures in the conversation (yours, from the history, and the ones
  the model looked at with `view_image`) are shown as thumbnails, and a click shows one large. Each picture is
  downloaded once and kept in the cache directory (`images/` in the data directory, private, at most 200 MiB).
* **The model.** The strip above the message box shows the model of the session. It can be changed while the
  session is idle, but only to a model that takes pictures if the current one does, and to one that does not if
  the current one does not (the backend refuses anything else). Entries that cannot be chosen are greyed out, and
  their tooltip says why. **Web search** is decided when the session is created, so it is only shown.
* **Options.** *Effort* (how hard the model thinks), and, behind the **⚙** button, the temperature and the most tokens
  to write. They are sent with every message, remembered for the session, and can also be chosen when a session is
  created. Anything not set uses the provider's default.
* **Continue.** If an answer ended in an error, a **Continue** button appears below the conversation. It sends "Please
  continue." as a new message.
* **Connection settings** (the **⚙** in the status bar, **Session ▸ Connection settings…**, Ctrl+,). The backend
  address and the token, a **Test connection** button, and **Reset to provisioned values**. A value that is the
  same as what the manager provided is not stored, so a reset really leaves no override. A value that an environment
  variable sets cannot be changed here, and the dialog says so. The dialog opens by itself when there is no token at
  all, and is offered when the backend refuses the token. Changing the address or token switches to the sessions of the
  new token: the others are kept, but not shown. An `http://` address beyond the private network gets a warning.
* **Keyboard.** Ctrl+N new session, Ctrl+K go to the list of sessions, Ctrl+L go to the message box, Ctrl+End jump to the
  latest message, Enter / Ctrl+Enter send, Shift+Enter new line, Esc stop, Ctrl+, connection settings, Ctrl+Q quit
  (also under **Help ▸ Keyboard shortcuts**). The window title names the session that is shown.

## When things go wrong

* **The backend does not answer.** The status bar says so, with a countdown to the next attempt and a
  **Retry now** button. The app looks again every 5 seconds meanwhile (every 30 seconds when all is well).
  The sidebar still shows your sessions, from the local store. When the backend is back, every conversation
  that was stuck (a history that did not load, a connection that waited for its next attempt) tries again
  **by itself**.
* **The token is refused (HTTP 401).** The status bar says "token rejected", and the conversations that are open
  say the same and can't send. Fix the token on the backend (or in the settings file), press **Retry now**,
  and they pick up again. Nothing is marked as missing because of it.
* **The connection drops in the middle of an answer.** The app reconnects and continues where it was, without losing
  or repeating text, and without running a tool call again. A banner shows what is going on, with **Retry now**.
* **A message whose answer was lost** is shown as "not confirmed" until the backend can be asked. Then it is either
  the committed message (it did arrive), or "not sent", with **Send again** and **Remove**. A message is
  never sent a second time on its own.
* **The app dies, or the VM shuts down.** On SIGTERM, SIGINT and SIGHUP the app quits properly and ends the commands
  of the model. After a crash, the journal tells what was done: finished results are sent again, and a command
  that was running is not run again. The journal does not depend on the token, so changing the token does not
  make a command run twice.
* **The journal cannot be written** (disk full, say): the command is **not** run, and the model is told why,
  instead of the session waiting for ever.
* **A bug in the app** that makes a conversation stop updating is retried three times. After that, the
  conversation says so and can be closed and opened again.

## Settings

Each key is resolved separately, highest priority first:

| Layer | Source | Notes |
| --- | --- | --- |
| 1 | `ETCODE_BACKEND_URL`, `ETCODE_API_TOKEN` | development and tests |
| 2 | `~/.config/etcode_desktop/config.json` | written by the GUI, mode 0600 |
| 3 | `/etc/etcode/desktop.json` | provisioned by the manager, read only (`ETCODE_PROVISIONED_FILE` overrides the path) |
| 4 | defaults | `http://10.0.2.2:8765` (the host, seen from a QEMU VM), no token |

Keys: `backend_url`, `token`, `vm_id` (stable UUID), and `max_parallel_tools` (deprecated: accepted and validated, but ignored; calls always run one at a time).
The manager provisions `vm_id` from the VM UUID. Outside a managed VM, the app creates and persists
one in its user config. Token rotation does not change this local identity.

An invalid value is logged and skipped, and the next layer is used.

The token can be read by anything running as the same user, including the model's
`bash` tool. Use a token that is dedicated to etcode VMs.

## Sessions

The backend still supports `GET /sessions`, but the desktop does not call it automatically.
The app keeps its own list in `store.sqlite3` (mode 0600), keyed by backend URL and stable VM ID,
not by bearer token. A VM therefore shows only chats it created or explicitly added by ID; a token
rotation does not switch chat lists. The backend remains the authority for whether the caller's
token may access a session.

* New session IDs are saved locally before the create flow returns. The sidebar uses this local
  list immediately, including when the backend is unreachable.
* At startup and every 30 seconds, the app refreshes metadata only for locally known session IDs
  with `GET /session/<id>`. It never discovers/imports chats through `GET /sessions`.
* A locally stored session that the backend answers 404 for is shown greyed out as "not on the
  backend". Connection errors never mark anything missing.
* To add a chat explicitly, use "Add a session by its id…" (the **⋯** button). The backend still
  enforces token ownership, so the adding VM's credential must already have access to that session.
  `GET /sessions` remains available to other API clients or for future manual recovery.
* "Remove from list" hides a session locally (the backend can't delete it). "Show removed sessions"
  brings it back. A title you typed is kept, and the backend's title is used otherwise.
* On a backend without `GET /sessions` the app refreshes the most recent stored sessions one by
  one, and "Add a session by its id…" (the **⋯** button) is the way to bring in other sessions.
* A new session's id is written to the store before anything else happens with it.

## Files

Standard XDG locations, all below an `etcode_desktop` directory: config in
`$XDG_CONFIG_HOME`, data in `$XDG_DATA_HOME`, `desktop.log` (rotated, mode 0600) in
`$XDG_STATE_HOME`. The single-instance lock lives in `$XDG_RUNTIME_DIR`. Set
`ETCODE_LOG_LEVEL` (for example `DEBUG`) to change the log level. The token is removed
from log output.

## Layout

```
etcode_desktop/
  config.py, paths.py, logging_setup.py   settings, files, logging (no Qt)
  core/                                   protocol and logic (must never import Qt)
    api.py                                BackendClient: one method per endpoint, typed errors, retries
    sse.py                                SSE parser and EventFollower (reconnects with Last-Event-ID)
    models.py, errors.py                  typed views of the JSON, exceptions
    store.py                              SessionStore: the local list of sessions (SQLite)
    sync.py                               SessionSync: refreshes locally known IDs, creates and explicitly attaches sessions
    controller.py                         SessionController: history, send, follow the stream, stop. Emits events
    options.py, attachments.py            the inference options of a session, pictures to attach
    tools/                                the tools the model can use: bash, files, images, the scheduler
    transcript.py                         TranscriptModel: what the conversation view shows
    session_state.py                      SessionState: transcript + status + cost + notices, built from events
  ui/                                     PySide6 widgets
    bridge.py                             runs backend calls on threads, reports back to the GUI thread
    dispatch.py                           hands work from threads to the GUI thread through a queue (no signals from threads)
    session_list.py, new_session_dialog.py, main_window.py, formatting.py
    chat_session.py, chat_pane.py         one open session, and its conversation view
    transcript_view.py, markdown_view.py, composer.py
    model_bar.py, options_dialog.py       the model picker and options above the message box
    connection_dialog.py                  the backend address and token
    image_loader.py                       pictures: download once, cache on disk, thumbnails, the large view
tests/
  fake_backend.py                         in-process imitation of the backend (scriptable, with fault injection)
  test_contract_real_backend.py           the same client calls against the real backend
```

## Using the backend client

```python
from etcode_desktop.core.api import BackendClient
from etcode_desktop.core.models import user_message
from etcode_desktop.core.sse import EventFollower

client = BackendClient.from_settings(settings)
session = client.create_session("arena-coding-router")
client.submit(session.id, [user_message("hello")])
follower = EventFollower(client, session.id)
for event in follower:                  # reconnects by itself, delivers each event once
    print(event.kind, event.data)       # the last one is "done"
print(follower.outcome)                 # "done", "no_active_generation" or "stopped"
```

Errors: `Unreachable` (no usable answer), `BackendError` and its subclasses `AuthError` (401),
`NotFound` (404), `Conflict` (409), `ServerError` (5xx), `Unsupported` (the backend is too old
for an endpoint), and `ProtocolError` (a reply that isn't what the API documents).
`follower.close()` can be called from another thread and ends a blocked read at once.

## Tests

```bash
pip install -e '.[dev]'     # pytest and Flask (for the fake backend)
python -m pytest
```

They run headless (`QT_QPA_PLATFORM=offscreen`) and use temporary directories, so they
don't touch your real settings. Tests that need Flask are skipped if it isn't importable.
`test_contract_real_backend.py` also needs the backend's own dependencies (pydantic,
filetype, python-dotenv) and finds the backend in `../etcode_backend`. It starts the real
Flask app and worker against a fake LLM server, so no paid API is called.
