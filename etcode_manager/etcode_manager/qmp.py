"""Minimal QMP (QEMU Machine Protocol) client.

Speaks the QMP JSON protocol over TCP or a Unix socket.  TCP loopback is
used by default because it works identically on Windows, Fedora and
Debian.  The client is synchronous and thread-safe (one command at a
time); QMP events received in between are collected in ``self.events``.
"""

from __future__ import annotations

import json
import socket
import threading


class QMPError(RuntimeError):
    """Raised when the QMP connection fails or a command errors out."""


class QMPClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, unix_path: str | None = None):
        self._host = host
        self._port = port
        self._unix_path = unix_path
        self._sock: socket.socket | None = None
        self._rfile = None
        self._lock = threading.Lock()
        self.events: list[dict] = []

    # -- connection ---------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> dict:
        """Connect and perform the QMP handshake. Returns the greeting."""
        try:
            if self._unix_path:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.settimeout(timeout)
                self._sock.connect(self._unix_path)
            else:
                self._sock = socket.create_connection((self._host, self._port), timeout=timeout)
        except OSError as exc:
            raise QMPError(f"cannot connect to QMP: {exc}") from exc
        self._sock.settimeout(timeout)
        self._rfile = self._sock.makefile("rb")

        greeting = self._read_message()
        if "QMP" not in greeting:
            self.close()
            raise QMPError(f"unexpected QMP greeting: {greeting!r}")
        self.command("qmp_capabilities")
        return greeting

    def close(self) -> None:
        if self._rfile is not None:
            try:
                self._rfile.close()
            except OSError:
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._rfile = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- commands -----------------------------------------------------------

    def command(self, execute: str, **arguments) -> object:
        """Execute a QMP command and return the value of its ``return`` key."""
        with self._lock:
            if self._sock is None:
                raise QMPError("not connected")
            request: dict = {"execute": execute}
            if arguments:
                request["arguments"] = arguments
            try:
                self._sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
            except OSError as exc:
                raise QMPError(f"send failed: {exc}") from exc

            while True:
                message = self._read_message()
                if "event" in message:
                    self.events.append(message)
                    continue
                if "error" in message:
                    desc = message["error"].get("desc", str(message["error"]))
                    raise QMPError(f"{execute}: {desc}")
                return message.get("return")

    def _read_message(self) -> dict:
        assert self._rfile is not None
        line = self._rfile.readline()
        if not line:
            raise QMPError("connection closed by QEMU")
        return json.loads(line.decode("utf-8"))

    # -- convenience --------------------------------------------------------

    def query_status(self) -> str:
        """Return the VM status ('running', 'shutdown', 'paused', ...)."""
        status = self.command("query-status")
        return status.get("status", "unknown") if isinstance(status, dict) else "unknown"

    def powerdown(self) -> None:
        """Request a graceful ACPI power button shutdown."""
        self.command("system_powerdown")

    def quit(self) -> None:
        """Tell QEMU to exit immediately (hard stop)."""
        self.command("quit")