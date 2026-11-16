"""Make sure only one window runs per user.

The first instance holds a ``QLockFile`` and listens on a local socket. A later
launch (autostart plus a click on the panel launcher, say) can't take the lock. It
sends ``raise`` to the first instance, which brings its window to the front, and
then exits.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Signal, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

RAISE_MESSAGE = b"raise\n"


def default_server_name(lock_path: Path) -> str:
    # Derived from the lock path, which is per user. Short, because unix socket paths are limited.
    return "etcode-desktop-" + hashlib.sha1(str(lock_path).encode()).hexdigest()[:10]


class SingleInstance(QObject):
    raise_requested = Signal()

    def __init__(self, lock_path: Path, server_name: str | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._lock_path = Path(lock_path)
        self._server_name = server_name or default_server_name(self._lock_path)
        self._lock: QLockFile | None = None
        self._server: QLocalServer | None = None

    def acquire(self, *, retries: int = 10, retry_delay_ms: int = 100) -> bool:
        """Return True if this is the first instance. Otherwise ask the running one to show itself."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self._lock_path))
        if lock.tryLock(0):
            self._lock = lock
            self._listen()
            return True
        self._ask_running_instance(retries, retry_delay_ms)
        return False

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None

    # --- first instance ---------------------------------------------------------

    def _listen(self) -> None:
        QLocalServer.removeServer(self._server_name)  # a stale socket from a crashed run
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self._server_name):
            # We hold the lock, so we still run. We just can't be raised by a later launch.
            log.warning("cannot listen on %s: %s", self._server_name, server.errorString())
            return
        server.newConnection.connect(self._on_connection)
        self._server = server

    @Slot()
    def _on_connection(self) -> None:
        assert self._server is not None
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            socket.disconnected.connect(socket.deleteLater)
            socket.readyRead.connect(self._on_ready_read)  # a slot of ours, not a lambda that holds the socket
            if socket.bytesAvailable():
                self._read(socket)

    @Slot()
    def _on_ready_read(self) -> None:
        socket = self.sender()
        if isinstance(socket, QLocalSocket):
            self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        data = bytes(socket.readAll().data())
        if b"raise" in data:
            self.raise_requested.emit()

    # --- later launches ---------------------------------------------------------

    def _ask_running_instance(self, retries: int, retry_delay_ms: int) -> None:
        from PySide6.QtCore import QThread

        socket = QLocalSocket()
        for _ in range(max(retries, 1)):
            socket.connectToServer(self._server_name)
            if socket.waitForConnected(retry_delay_ms):
                socket.write(RAISE_MESSAGE)
                socket.waitForBytesWritten(1000)
                socket.disconnectFromServer()
                return
            # The first instance may hold the lock a moment before it listens.
            QThread.msleep(retry_delay_ms)
        log.warning("another instance holds %s but does not answer on %s", self._lock_path, self._server_name)
