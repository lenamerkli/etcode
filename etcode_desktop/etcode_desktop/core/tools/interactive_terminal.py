"""Persistent, per-chat PTY terminals used by the interactive_terminal tool."""
from __future__ import annotations

import codecs
import ctypes
import fcntl
import json
import logging
import math
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import threading
import time
from dataclasses import dataclass
from typing import Any

import pyte

from .cancel import CancelToken

log = logging.getLogger(__name__)
DEFAULT_HEIGHT = 50
DEFAULT_WIDTH = 160
MAX_HEIGHT = 200
MAX_WIDTH = 400
MAX_ACTIONS = 100
MAX_WAIT_SECONDS = 300.0
MAX_TOTAL_WAIT_SECONDS = 600.0
MAX_ACTIONS_TEXT = 20_000
READ_SIZE = 65536


class TerminalError(ValueError):
    """A terminal action or session cannot be performed."""


@dataclass(frozen=True)
class Action:
    name: str
    value: Any = None


def _split_actions(text: str) -> list[str]:
    if not isinstance(text, str) or len(text) > MAX_ACTIONS_TEXT:
        raise TerminalError(f"actions must be text no longer than {MAX_ACTIONS_TEXT} characters")
    pieces: list[str] = []
    start = 0
    quoted = escaped = False
    depth = 0
    for i, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise TerminalError("unexpected ')' in actions")
        elif char == "," and depth == 0:
            piece = text[start:i].strip()
            if piece:
                pieces.append(piece)
            start = i + 1
    if quoted or depth != 0:
        raise TerminalError("unterminated string or action in actions")
    piece = text[start:].strip()
    if piece:
        pieces.append(piece)
    if len(pieces) > MAX_ACTIONS:
        raise TerminalError(f"actions may contain at most {MAX_ACTIONS} actions")
    return pieces


def parse_actions(text: str) -> list[Action]:
    result: list[Action] = []
    for item in _split_actions(text):
        match = re.fullmatch(r"(type|press|wait|print)\((.*)\)", item, re.DOTALL)
        if not match:
            raise TerminalError(f"invalid action {item[:100]!r}")
        name, argument = match.group(1), match.group(2).strip()
        if name in ("type", "press"):
            try:
                value = json.loads(argument)
            except (json.JSONDecodeError, TypeError) as exc:
                raise TerminalError(f"{name}() requires a JSON string") from exc
            if not isinstance(value, str):
                raise TerminalError(f"{name}() requires a JSON string")
            if name == "press":
                _key_bytes(value)  # validate before executing any preceding actions
        elif name == "wait":
            try:
                value = float(argument)
            except ValueError as exc:
                raise TerminalError("wait() requires a number of seconds") from exc
            if not math.isfinite(value) or value < 0 or value > MAX_WAIT_SECONDS:
                raise TerminalError(f"wait() must be between 0 and {MAX_WAIT_SECONDS:g} seconds")
        else:
            if argument not in ("", "color"):
                raise TerminalError("print() accepts no argument or print(color)")
            value = argument == "color"
        result.append(Action(name, value))
    if not result:
        raise TerminalError("actions must contain at least one action")
    if sum(a.value for a in result if a.name == "wait") > MAX_TOTAL_WAIT_SECONDS:
        raise TerminalError(f"total wait time may not exceed {MAX_TOTAL_WAIT_SECONDS:g} seconds")
    return result


_KEY_BYTES = {
    "ENTER": b"\r", "TAB": b"\t", "BACKSPACE": b"\x7f", "DELETE": b"\x1b[3~",
    "ESC": b"\x1b", "SPACE": b" ", "ARROWUP": b"\x1b[A", "ARROWDOWN": b"\x1b[B",
    "ARROWRIGHT": b"\x1b[C", "ARROWLEFT": b"\x1b[D", "HOME": b"\x1b[H", "END": b"\x1b[F",
    "PAGEUP": b"\x1b[5~", "PAGEDOWN": b"\x1b[6~", "INSERT": b"\x1b[2~",
    "F1": b"\x1bOP", "F2": b"\x1bOQ", "F3": b"\x1bOR", "F4": b"\x1bOS",
    "F5": b"\x1b[15~", "F6": b"\x1b[17~", "F7": b"\x1b[18~", "F8": b"\x1b[19~",
    "F9": b"\x1b[20~", "F10": b"\x1b[21~", "F11": b"\x1b[23~", "F12": b"\x1b[24~",
}
_MODIFIERS = {"CTRL", "ALT", "ALTGR", "SHIFT", "SUPER"}


def _key_bytes(spec: str) -> bytes:
    parts = spec.split("+")
    if not parts or any(not part for part in parts):
        raise TerminalError(f"invalid key name {spec!r}")
    if len(parts) == 1 and parts[0].upper() == "PLUS":
        return b"+"
    modifiers = {part.upper() for part in parts[:-1]}
    key_raw, key = parts[-1], parts[-1].upper()
    if any(mod not in _MODIFIERS for mod in modifiers) or len(modifiers) != len(parts) - 1:
        raise TerminalError(f"unsupported key combination {spec!r}")
    if key in _MODIFIERS or len(modifiers) > 3:
        raise TerminalError(f"invalid key combination {spec!r}")
    if key in _KEY_BYTES:
        value = _KEY_BYTES[key]
        if modifiers:
            modifier = 1 + int("SHIFT" in modifiers) + 2 * int(bool(modifiers & {"ALT", "ALTGR"})) \
                + 4 * int("CTRL" in modifiers) + 8 * int("SUPER" in modifiers)
            cursors = {"ARROWUP": "A", "ARROWDOWN": "B", "ARROWRIGHT": "C",
                       "ARROWLEFT": "D", "HOME": "H", "END": "F"}
            if key in cursors:
                return f"\x1b[1;{modifier}{cursors[key]}".encode()
            tilde_codes = {"DELETE": 3, "INSERT": 2, "PAGEUP": 5, "PAGEDOWN": 6,
                           "F5": 15, "F6": 17, "F7": 18, "F8": 19,
                           "F9": 20, "F10": 21, "F11": 23, "F12": 24}
            if key in tilde_codes:
                return f"\x1b[{tilde_codes[key]};{modifier}~".encode()
            ss3 = {"F1": "P", "F2": "Q", "F3": "R", "F4": "S"}
            if key in ss3:
                return f"\x1b[1;{modifier}{ss3[key]}".encode()
            if key == "ENTER":
                return f"\x1b[13;{modifier}u".encode()
            if key == "TAB":
                return f"\x1b[9;{modifier}u".encode()
            if "ALT" in modifiers or "ALTGR" in modifiers:
                return b"\x1b" + value
        return value
    if len(key_raw) != 1 or not key_raw.isprintable():
        raise TerminalError(f"unsupported key {spec!r}")
    char = key_raw
    if "SHIFT" in modifiers:
        char = char.upper()
    if "CTRL" in modifiers:
        code = ord(char.upper())
        if 64 <= code <= 95:
            char = chr(code - 64)
        elif char == "?":
            char = "\x7f"
        else:
            raise TerminalError(f"CTRL cannot be combined with {spec!r}")
    data = char.encode("utf-8")
    if "ALT" in modifiers or "ALTGR" in modifiers:
        data = b"\x1b" + data
    return data


class _PTYSession:
    def __init__(self, height: int, width: int):
        self.height, self.width = height, width
        self.screen = pyte.Screen(width, height)
        self.stream = pyte.Stream(self.screen)
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._closed = threading.Event()
        desktop_pid = os.getpid()
        self.pid, self._master = pty.fork()
        if self.pid == 0:  # child: pty.fork creates a new session and controlling terminal
            try:
                # If the desktop crashes, ensure the shell receives a signal too. Graceful
                # shutdown additionally kills its whole process group in close().
                if sys.platform.startswith("linux"):
                    ctypes.CDLL(None).prctl(1, signal.SIGTERM)
                    if os.getppid() != desktop_pid:  # close the fork/prctl race
                        os.kill(os.getpid(), signal.SIGTERM)
                env = dict(os.environ)
                env.pop("ETCODE_API_TOKEN", None)
                env.update({"TERM": "xterm-256color", "PS1": "agent$ ", "PAGER": "cat"})
                os.execvpe("bash", ["bash", "--noprofile", "--norc", "-i"], env)
            except BaseException:
                os._exit(127)
        self._resize_fd(height, width)
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name=f"interactive-terminal-{self.pid}")
        self._reader.start()

    def _resize_fd(self, height: int, width: int) -> None:
        fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        with self._lock:
            self.screen.resize(lines=height, columns=width)
        try:
            os.kill(self.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    def resize(self, height: int, width: int) -> None:
        if self._closed.is_set():
            raise TerminalError("terminal process has exited")
        self._resize_fd(height, width)
        self.height, self.width = height, width

    def _read_loop(self) -> None:
        while not self._closed.is_set():
            try:
                readable, _, _ = select.select([self._master], [], [], 0.1)
                if not readable:
                    try:
                        pid, _ = os.waitpid(self.pid, os.WNOHANG)
                    except ChildProcessError:
                        pid = self.pid
                    if pid:
                        self._closed.set()
                        break
                    continue
                data = os.read(self._master, READ_SIZE)
                if not data:
                    break
                text = self._decoder.decode(data)
                with self._lock:
                    self.stream.feed(text)
            except (OSError, ValueError):
                break
        self._closed.set()

    def write(self, data: bytes) -> None:
        if self._closed.is_set():
            raise TerminalError("terminal process has exited")
        with self._write_lock:
            try:
                os.write(self._master, data)
            except OSError as exc:
                raise TerminalError("terminal process has exited") from exc

    def snapshot(self, color: bool) -> str:
        with self._lock:
            if not color:
                return "\n".join(line.rstrip() for line in self.screen.display).rstrip()
            return _colored_display(self.screen)

    def close(self) -> None:
        if self._closed.is_set():
            try:
                os.close(self._master)
            except OSError:
                pass
            return
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
                if pid:
                    break
            except ChildProcessError:
                break
            time.sleep(0.02)
        else:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._closed.set()
        try:
            os.close(self._master)
        except OSError:
            pass
        if self._reader.is_alive() and threading.current_thread() is not self._reader:
            self._reader.join(timeout=0.3)


def _ansi_color(value: str, foreground: bool) -> str | None:
    if value in ("default", "", None):
        return None
    basic = {"black": 0, "red": 1, "green": 2, "brown": 3, "yellow": 3, "blue": 4,
             "magenta": 5, "purple": 5, "cyan": 6, "white": 7}
    prefix = 30 if foreground else 40
    if value in basic:
        return str(prefix + basic[value])
    low = value.lower()
    if low.startswith("bright") and low[6:] in basic:
        return str((90 if foreground else 100) + basic[low[6:]])
    if value.isdecimal():
        return f"{38 if foreground else 48};5;{value}"
    return None


def _colored_display(screen: pyte.Screen) -> str:
    lines: list[str] = []
    for y in range(screen.lines):
        row = screen.buffer[y]
        out: list[str] = []
        style: tuple[Any, ...] | None = None
        current_codes: tuple[str, ...] = ()
        for x in range(screen.columns):
            char = row[x]
            attrs = (char.fg, char.bg, char.bold, char.italics, char.underscore, char.reverse)
            codes = tuple(c for c in (_ansi_color(char.fg, True), _ansi_color(char.bg, False),
                                      "1" if char.bold else None, "3" if char.italics else None,
                                      "4" if char.underscore else None, "7" if char.reverse else None) if c)
            if attrs != style:
                out.append("\x1b[0m" + ("\x1b[" + ";".join(codes) + "m" if codes else ""))
                style, current_codes = attrs, codes
            out.append(char.data)
        out.append("\x1b[0m")
        lines.append("".join(out).rstrip())
    return "\n".join(lines).rstrip()


class TerminalManager:
    """Owns live terminals and records cleaned IDs in the local session store."""

    def __init__(self, store=None):
        self.store = store
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, str, str], _PTYSession] = {}
        self._shutdown = False
        if store is not None:
            # Any active row belongs to a prior process; there is no PTY reattachment.
            store.terminal_clean_all_active()

    @staticmethod
    def _validate_size(height: int | None, width: int | None, *, new: bool,
                       old_height: int | None = None, old_width: int | None = None) -> tuple[int, int]:
        h = height if height is not None else (old_height if old_height is not None else DEFAULT_HEIGHT)
        w = width if width is not None else (old_width if old_width is not None else DEFAULT_WIDTH)
        if not 1 <= h <= MAX_HEIGHT:
            raise TerminalError(f"height must be between 1 and {MAX_HEIGHT}")
        if not 1 <= w <= MAX_WIDTH:
            raise TerminalError(f"width must be between 1 and {MAX_WIDTH}")
        return h, w

    def run(self, *, chat_id: str, backend_url: str, terminal_id: str, actions: str,
            height: int | None, width: int | None, cancel: CancelToken) -> str:
        if not chat_id or not backend_url:
            raise TerminalError("terminal session needs a chat and backend identity")
        if not terminal_id or len(terminal_id) > 128:
            raise TerminalError("session must be a non-empty terminal ID of at most 128 characters")
        parsed = parse_actions(actions)
        key = (backend_url.rstrip("/"), chat_id, terminal_id)
        with self._lock:
            if self._shutdown:
                raise TerminalError("terminal manager is shutting down")
            session = self._sessions.get(key)
            if session is None:
                h, w = self._validate_size(height, width, new=True)
                if self.store is not None:
                    state = self.store.terminal_state(*key)
                    if state == "cleaned":
                        raise TerminalError(f"terminal session {terminal_id!r} was cleaned up when etcode-desktop last exited; use a new session ID")
                    if state == "active":
                        raise TerminalError(f"terminal session {terminal_id!r} was active in another desktop process and has been cleaned up; use a new session ID")
                    if not self.store.terminal_start(*key):
                        raise TerminalError(f"terminal session ID {terminal_id!r} has already been used; use a new session ID")
                try:
                    session = _PTYSession(h, w)
                except Exception:
                    if self.store is not None:
                        self.store.terminal_forget_failed_start(*key)
                    raise
                self._sessions[key] = session
            else:
                h, w = self._validate_size(height, width, new=False,
                                           old_height=session.height, old_width=session.width)
                if (h, w) != (session.height, session.width):
                    session.resize(h, w)
        snapshots: list[str] = []
        try:
            for action in parsed:
                if cancel.cancelled:
                    raise TerminalError("terminal actions were cancelled")
                if session._closed.is_set():
                    raise TerminalError(f"terminal process for session {terminal_id!r} has exited; use a new session ID")
                if action.name == "type":
                    session.write(action.value.encode("utf-8"))
                elif action.name == "press":
                    session.write(_key_bytes(action.value))
                elif action.name == "wait":
                    if cancel.wait(action.value):
                        raise TerminalError("terminal actions were cancelled")
                else:
                    snapshots.append(session.snapshot(action.value))
            if not snapshots:
                snapshots.append(session.snapshot(False))
            return "\n\n".join(snapshots) or "(terminal screen is empty)"
        except TerminalError:
            if session._closed.is_set():
                self._retire(key, session)
            raise

    def _retire(self, key: tuple[str, str, str], session: _PTYSession) -> None:
        with self._lock:
            if self._sessions.get(key) is session:
                self._sessions.pop(key, None)
                session.close()
                if self.store is not None:
                    self.store.terminal_clean(*key)

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            sessions = list(self._sessions.items())
            self._sessions.clear()
        for key, session in sessions:
            session.close()
            if self.store is not None:
                try:
                    self.store.terminal_clean(*key)
                except Exception:
                    log.exception("could not record cleanup of terminal session %s", key[2])
