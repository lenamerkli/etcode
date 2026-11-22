"""The live test uses a throwaway X server, never the developer's desktop."""
from __future__ import annotations

import ctypes
import os
import select
import shutil
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace

import pytest

from etcode_desktop.core.tools import ToolContext, execute_tool


def call(action, context=None, **args):
    return execute_tool("interactive_gui", {"action": action, **args}, context or ToolContext())


def message(parts):
    return parts[-1]["text"]


def test_requires_x11(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    assert "DISPLAY is missing" in message(call("list_windows"))
    assert "unknown GUI action" in message(call("launch"))


@pytest.fixture
def x11(monkeypatch):
    for name in ("Xvfb", "openbox", "wmctrl", "xdotool", "xwininfo", "xprop", "xev", "import"):
        if not shutil.which(name):
            pytest.skip(f"live GUI test needs {name}")
    with ExitStack() as stack:
        def start(*args, **kwargs):
            proc = subprocess.Popen(args, stderr=subprocess.DEVNULL, **kwargs)
            stack.callback(proc.wait)
            stack.callback(proc.terminate)
            return proc

        with tempfile_pipe() as (fd, reader):
            start("Xvfb", "-displayfd", str(fd), "-screen", "0", "1024x768x24", "-nolisten", "tcp", "-noreset", pass_fds=(fd,))
            assert select.select([reader], [], [], 5)[0], "Xvfb did not start"
            display = ":" + reader.readline().strip()
        monkeypatch.setenv("DISPLAY", display)
        monkeypatch.delenv("XAUTHORITY", raising=False)
        start("openbox", "--sm-disable", stdout=subprocess.DEVNULL)
        eventually(lambda: subprocess.run(["wmctrl", "-m"], capture_output=True).returncode == 0)
        for title, at in (("et-gui-test", "+40+50"), ("et-gui-other", "+400+300")):
            start("xev", "-name", title, "-geometry", "240x180" + at, stdout=subprocess.DEVNULL)
            def find_xev():
                result = subprocess.run(["xdotool", "search", "--name", f"^{title}$"], capture_output=True, text=True)
                return result.stdout.strip()
            eventually(find_xev)
            # Some wmctrl releases crash when every client omits WM_CLIENT_MACHINE (as xev does).
            subprocess.run(["xprop", "-id", find_xev(), "-f", "WM_CLIENT_MACHINE", "8s",
                            "-set", "WM_CLIENT_MACHINE", "test"], check=True)
        yield


@contextmanager
def tempfile_pipe():
    read, write = os.pipe()
    try:
        with os.fdopen(read) as reader:
            yield write, reader
    finally:
        os.close(write)


def eventually(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        time.sleep(0.02)


def test_windows_screenshot_and_input(x11):
    def find(title):
        return next((line.split()[0] for line in message(call("list_windows")).splitlines() if title in line), None)

    eventually(lambda: find("et-gui-test") and find("et-gui-other"))
    window, other = find("et-gui-test"), find("et-gui-other")
    assert "run list_windows again" in message(call("click", window_id="0xffff"))
    assert "hex ID" in message(call("click", window_id="-invalid"))
    assert "x must be" in message(call("click", window_id=window, x=240, y=20))
    assert "completed" in message(call("click", window_id=window, x=60, y=60))
    assert "completed" in message(call("scroll", window_id=window, ticks=2))
    assert "completed" in message(call("type", window_id=window, text="--literal"))
    assert "completed" in message(call("press", window_id=window, key="ctrl+l"))
    assert "No such key name" in message(call("press", window_id=window, key="NotAnX11Key"))

    def pointer():
        x, y, *_ = subprocess.check_output(["xdotool", "getmouselocation"], text=True).split()
        return int(x[2:]), int(y[2:])

    x, y = pointer()
    assert "completed" in message(call("move_relative", window_id=window, dx=-10, dy=5, steps=2))
    assert pointer() == (x - 20, y + 10)

    class Uploader:
        def upload_image(self, data, name):
            assert name == f"window-{window}.png"
            assert data.startswith(b"\x89PNG\r\n\x1a\n")
            assert struct.unpack(">II", data[16:24]) == (240, 180)
            return SimpleNamespace(hash="a" * 64)

    result = call("screenshot", ToolContext(client=Uploader()), window_id=window)
    assert result[0]["type"] == "image", result

    # Look at the server's logical keyboard state, not just the tool's result:
    # a lost keyup would leave W stuck in whichever app the user takes over.
    lib = ctypes.CDLL("libX11.so.6")
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.XKeysymToKeycode.restype = ctypes.c_ubyte
    display = lib.XOpenDisplay(None)
    assert display
    try:
        key = lib.XKeysymToKeycode(display, ord("w"))

        def down():
            bits = (ctypes.c_ubyte * 32)()
            lib.XQueryKeymap(display, bits)
            return bool(bits[key // 8] & (1 << (key % 8)))

        with ThreadPoolExecutor(max_workers=1) as pool:
            ctx = ToolContext()
            hold = pool.submit(call, "hold", ctx, window_id=window, key="w", duration_ms=2000)
            eventually(down)
            assert "another GUI action" in message(call("press", window_id=other, key="a"))
            ctx.cancel.cancel()
            assert "cancelled" in message(hold.result(timeout=1))
            assert not down()

            hold = pool.submit(call, "hold", window_id=window, key="w", duration_ms=2000)
            eventually(down)
            subprocess.run(["wmctrl", "-i", "-a", other], check=True)
            assert "lost keyboard focus" in message(hold.result(timeout=1))
            assert not down()
    finally:
        lib.XCloseDisplay(display)

    subprocess.run(["xdotool", "windowmove", window, "-70", "70"], check=True)
    eventually(lambda: "Absolute upper-left X:  -" in subprocess.check_output(["xwininfo", "-id", window], text=True))
    assert "screenshot was clipped" in message(call("screenshot", ToolContext(client=Uploader()), window_id=window))

    subprocess.run(["wmctrl", "-i", "-c", window], check=True)
    eventually(lambda: not find("et-gui-test"))
    assert "run list_windows again" in message(call("hold", window_id=window, key="w", duration_ms=1))
