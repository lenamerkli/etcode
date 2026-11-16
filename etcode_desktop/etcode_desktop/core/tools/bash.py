"""The ``bash`` tool.

The command runs as ``bash -c <command>`` in a process group of its own, so that a timeout or Stop can
end the command and everything it started. Output is read from both pipes by two threads, with a hard cap
on what is kept, so that a command which prints without end cannot fill the memory.

The result text is exactly::

    <returncode>{code}</returncode>
    <stderr>{stderr}</stderr>
    <stdout>{stdout}</stdout>

cut to ``max_chars`` (with a note about what was cut).
"""
from __future__ import annotations

import os
import select
import signal
import subprocess
import threading
import time
from pathlib import Path

from .cancel import CancelToken

TERMINATE_GRACE_SECONDS = 2.0  # between SIGTERM and SIGKILL
PIPE_DRAIN_SECONDS = 0.3  # how long to keep reading after the command is gone, for output that is still in the pipes
POLL_SECONDS = 0.05
READ_CHUNK = 65536
BYTES_PER_CHAR = 4  # the most that a character takes in UTF-8
SECRET_VARIABLES = ("ETCODE_API_TOKEN",)  # the command does not need our credentials


class _PipeReader(threading.Thread):
    """Collects what a pipe delivers, up to a limit. The rest is read and thrown away, so that the command never blocks."""

    def __init__(self, fd: int, limit: int, name: str):
        super().__init__(daemon=True, name=name)
        self._fd = fd
        self._limit = limit
        self._buffer = bytearray()
        self.dropped = 0
        self._exited_at: float | None = None

    def command_exited(self) -> None:
        self._exited_at = time.monotonic()

    def run(self) -> None:
        while True:
            ready, _, _ = select.select([self._fd], [], [], POLL_SECONDS)
            if ready:
                try:
                    chunk = os.read(self._fd, READ_CHUNK)
                except OSError:
                    return
                if not chunk:
                    return  # end of the stream
                room = self._limit - len(self._buffer)
                if room > 0:
                    self._buffer += chunk[:room]
                self.dropped += max(len(chunk) - max(room, 0), 0)
            elif self._exited_at is not None and time.monotonic() - self._exited_at > PIPE_DRAIN_SECONDS:
                return  # the command is gone and nothing more arrives: a background process may still hold the pipe
            if self._exited_at is not None and time.monotonic() - self._exited_at > PIPE_DRAIN_SECONDS * 4:
                return  # a background process that keeps printing must not keep us here either

    def text(self) -> str:
        return self._buffer.decode("utf-8", errors="replace")


def _signal_group(process: subprocess.Popen, signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except (ProcessLookupError, PermissionError):
        pass  # already gone


def terminate(process: subprocess.Popen) -> None:
    """End the command and everything it started: SIGTERM, then SIGKILL if it does not obey."""
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(process, signal.SIGKILL)  # also reaches children that ignored SIGTERM
    try:
        process.wait(TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def format_result(code: int | str, stderr: str, stdout: str) -> str:
    return f"<returncode>{code}</returncode>\n<stderr>{stderr}</stderr>\n<stdout>{stdout}</stdout>"


def _cut(output: str, max_chars: int, dropped_chars: int) -> str:
    total = len(output) + dropped_chars
    if total <= max_chars:
        return output
    return output[:max_chars] + f"\n[output truncated, {total - max_chars} more characters]"


def _with_note(stderr: str, note: str) -> str:
    return stderr + ("\n" if stderr and not stderr.endswith("\n") else "") + note


def environment(venv: str | None) -> dict[str, str]:
    env = dict(os.environ)
    for name in SECRET_VARIABLES:
        env.pop(name, None)
    if venv:
        env["VIRTUAL_ENV"] = venv
        env["PATH"] = str(Path(venv) / "bin") + os.pathsep + env.get("PATH", "")
        env.pop("PYTHONHOME", None)
    return env


def run_bash(command: str, timeout: int, directory: Path, venv: Path | None, max_chars: int, cancel: CancelToken) -> str:
    """Run the command and return the result text. Never raises for a failing command."""
    try:
        process = subprocess.Popen(
            ["bash", "-c", command], cwd=directory, env=environment(str(venv) if venv else None),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        return _cut(format_result(-1, f"cannot start the command: {exc}", ""), max_chars, 0)

    limit = max_chars * BYTES_PER_CHAR
    assert process.stdout is not None and process.stderr is not None
    readers = (_PipeReader(process.stdout.fileno(), limit, "bash-stdout"), _PipeReader(process.stderr.fileno(), limit, "bash-stderr"))
    for reader in readers:
        reader.start()

    stop_by_cancel = threading.Event()

    def on_cancel() -> None:
        stop_by_cancel.set()
        threading.Thread(target=terminate, args=(process,), daemon=True, name="bash-cancel").start()

    unregister = cancel.on_cancel(on_cancel)
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while True:
            try:
                process.wait(min(POLL_SECONDS * 2, max(deadline - time.monotonic(), 0.001)))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline and not stop_by_cancel.is_set():
                    timed_out = True
                    terminate(process)
                    break
    finally:
        unregister()
        if process.poll() is None:  # still running: only possible if we are leaving early
            terminate(process)
        for reader in readers:
            reader.command_exited()
        for reader in readers:
            reader.join(PIPE_DRAIN_SECONDS * 8)
        process.stdout.close()
        process.stderr.close()

    stderr, stdout = readers[1].text(), readers[0].text()
    if timed_out:
        stderr = _with_note(stderr, f"Command timed out after {timeout} seconds")
    elif stop_by_cancel.is_set():
        stderr = _with_note(stderr, "Command was cancelled")
    output = format_result(process.returncode, stderr, stdout)
    return _cut(output, max_chars, readers[0].dropped + readers[1].dropped)

