"""Window-scoped input and screenshots on the logged-in guest's X11 desktop."""
from __future__ import annotations

import os
import re
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..api import BackendClient
from ..models import text_part
from .bash import environment
from .cancel import CancelToken
from .files import FileToolError
from .image import view_image
from .schemas import ToolArgumentError

_ACTIONS = {"list_windows", "screenshot", "click", "scroll", "type", "press", "hold", "move_relative"}
_XID = re.compile(r"0x[0-9a-fA-F]{1,8}")
_KEY = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|[0-9]")
_DESKTOP = threading.Lock()  # tool rounds in different chats otherwise run concurrently


def _command(*argv: str, timeout: float = 2) -> str:
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env={**environment(None), "LC_ALL": "C.UTF-8"})
    except FileNotFoundError:
        raise FileToolError(f"{argv[0]} is missing; install the interactive GUI guest dependencies") from None
    except subprocess.TimeoutExpired:
        raise FileToolError(f"{argv[0]} timed out") from None
    if proc.returncode or "(symbol) No such key name" in proc.stderr:
        raise FileToolError(f"{argv[0]} failed: {proc.stderr.strip()[:500] or proc.returncode}")
    return proc.stdout


def _running(cancel: CancelToken) -> None:
    if cancel.cancelled:
        raise FileToolError("GUI action was cancelled")


def _windows() -> dict[int, str]:
    windows = {}
    for line in _command("wmctrl", "-l").splitlines():
        fields = line.split(maxsplit=3)
        if len(fields) >= 3 and _XID.fullmatch(fields[0]):
            windows[int(fields[0], 16)] = fields[3] if len(fields) == 4 else "(untitled)"
    return windows


def _active(window: int) -> bool:
    return _command("xdotool", "getactivewindow").strip() == str(window)


def _focused(window: int, cancel: CancelToken) -> None:
    _running(cancel)
    if not _active(window) or _command("xdotool", "getwindowfocus").strip() != str(window):
        raise FileToolError("the target window lost keyboard focus; no further input was sent")
    _running(cancel)


def _activate(window: int, cancel: CancelToken) -> None:
    _running(cancel)
    if not _active(window):
        _command("wmctrl", "-i", "-a", hex(window))
        deadline = time.monotonic() + 1
        while not _active(window):
            if cancel.wait(0.025):
                _running(cancel)
            if time.monotonic() >= deadline:
                raise FileToolError("the window manager did not activate the target window")
    _running(cancel)


def _properties(output: str) -> dict[str, int]:
    return {key: int(value) for key, value in re.findall(r"^([A-Z]+)=(-?\d+)$", output, re.M)}


def _geometry(window: int) -> dict[str, int]:
    return _properties(_command("xdotool", "getwindowgeometry", "--shell", hex(window)))


def _pointer_over(window: int) -> None:
    # xdotool reports the root child beneath the pointer, which can be a WM frame.
    # Walk up from the client so an always-on-top window won't receive our click.
    pointed = _properties(_command("xdotool", "getmouselocation", "--shell")).get("WINDOW")
    child = window
    for _ in range(8):
        if pointed == child:
            return
        tree = _command("xwininfo", "-id", hex(child), "-tree")
        root = re.search(r"Root window id:\s*(0x[0-9a-f]+)", tree)
        parent = re.search(r"Parent window id:\s*(0x[0-9a-f]+)", tree)
        if not parent or not root or parent[1] == root[1]:
            break
        child = int(parent[1], 16)
    raise FileToolError("the pointer is outside the target window or another window covers it")


def _hold(window: int, key: str, seconds: float, cancel: CancelToken) -> None:
    # Release from Stop/close synchronously, even if the tool worker is a daemon.
    gate = threading.Lock()
    down = False

    def release() -> None:
        nonlocal down
        with gate:
            if down:
                _command("xdotool", "keyup", key)
                down = False

    remove = cancel.on_cancel(release)
    try:
        _focused(window, cancel)
        with gate:
            _running(cancel)
            down = True  # keydown can partially succeed for a chord
            _command("xdotool", "keydown", key)
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            cancel.wait(min(0.05, remaining))
            _focused(window, cancel)
    finally:
        try:
            release()
        finally:
            remove()


def _integer(args: dict[str, Any], name: str, lower: int, upper: int) -> int:
    value = args.get(name)
    if value is None or not lower <= value <= upper:
        raise ToolArgumentError(f"{name} must be between {lower} and {upper}")
    return value


def _input(window: int, action: str, args: dict[str, Any], cancel: CancelToken) -> None:
    if action in {"press", "hold"}:
        key = args.get("key", "")
        keys = key.split("+")
        if len(keys) > 4 or len(key) > 80 or not all(_KEY.fullmatch(part) for part in keys):
            raise ToolArgumentError("key must be an X11 key name or a chord such as ctrl+l")
        seconds = _integer(args, "duration_ms", 1, 2000) / 1000 if action == "hold" else 0.025
        _activate(window, cancel)
        _hold(window, key, seconds, cancel)
        return
    if action == "type":
        text = args.get("text", "")
        if not 1 <= len(text) <= 256 or "\0" in text:
            raise ToolArgumentError("text must be 1 to 256 characters without NULs")
        _activate(window, cancel)
        _focused(window, cancel)
        _command("xdotool", "type", "--delay", "0", "--", text)
        return
    if action == "click":
        geometry = _geometry(window)
        x = _integer(args, "x", 0, geometry.get("WIDTH", 0) - 1)
        y = _integer(args, "y", 0, geometry.get("HEIGHT", 0) - 1)
        button = _integer(args, "button", 1, 3)
        _activate(window, cancel)
        _focused(window, cancel)
        _command("xdotool", "mousemove", "--window", hex(window), str(x), str(y))
        _pointer_over(window)
        _focused(window, cancel)
        _command("xdotool", "click", str(button))
        return
    if action == "scroll":
        ticks = _integer(args, "ticks", -10, 10)
        if ticks == 0:
            raise ToolArgumentError("ticks must not be zero")
        _activate(window, cancel)
        for _ in range(abs(ticks)):
            _pointer_over(window)
            _focused(window, cancel)
            _command("xdotool", "click", "4" if ticks < 0 else "5")
        return
    dx = _integer(args, "dx", -500, 500)
    dy = _integer(args, "dy", -500, 500)
    steps = _integer(args, "steps", 1, 20)
    _activate(window, cancel)
    for step in range(steps):
        if step:
            cancel.wait(0.05)
        _focused(window, cancel)
        _command("xdotool", "mousemove_relative", "--", str(dx), str(dy))


def run_gui(args: dict[str, Any], cancel: CancelToken, client: BackendClient | None) -> list[dict[str, Any]]:
    action = args["action"]
    if action not in _ACTIONS:
        raise ToolArgumentError(f"unknown GUI action {action!r}")
    _running(cancel)
    if not os.environ.get("DISPLAY"):
        raise FileToolError("interactive GUI requires a logged-in X11 desktop (DISPLAY is missing)")
    if action == "list_windows":
        windows = _windows()
        return [text_part("\n".join(f"{window:#010x} {title}" for window, title in windows.items())
                          or "No managed windows are open.")]
    xid = args.get("window_id", "")
    if not _XID.fullmatch(xid):
        raise ToolArgumentError("window_id must be a hex ID from list_windows")
    window = int(xid, 16)
    if action == "screenshot" and client is None:
        raise FileToolError("screenshots cannot be uploaded: there is no connection to the backend")
    if not _DESKTOP.acquire(blocking=False):
        raise FileToolError("another GUI action is using the desktop; try again once it finishes")
    try:
        _running(cancel)
        if window not in _windows():
            raise FileToolError("window is no longer listed; run list_windows again")
        if action != "screenshot":
            _input(window, action, args, cancel)
            _running(cancel)
            return [text_part(f"{action} completed for {window:#010x}.")]
        with tempfile.TemporaryDirectory(prefix="etcode-gui-") as tmp:
            screenshot = Path(tmp) / f"window-{window:#010x}.png"
            _activate(window, cancel)
            geometry = _geometry(window)
            _command("import", "-silent", "-window", hex(window), str(screenshot), timeout=5)
            with screenshot.open("rb") as png:
                header = png.read(24)
            if len(header) != 24 or struct.unpack(">II", header[16:]) != (geometry.get("WIDTH"), geometry.get("HEIGHT")):
                raise FileToolError("the screenshot was clipped or the window resized; move it fully onscreen and retry")
            _running(cancel)
            if not _active(window):
                raise FileToolError("the target window lost focus during capture; take another screenshot")
            result = view_image(str(screenshot), Path(tmp), client)
            return [result[0], text_part(f"Client-area screenshot of {window:#010x}.")]
    finally:
        _DESKTOP.release()
