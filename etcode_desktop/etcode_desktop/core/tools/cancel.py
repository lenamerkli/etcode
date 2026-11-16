"""A flag that Stop sets, which running tools watch. Callbacks let a tool react at once (kill a process)."""
from __future__ import annotations

import threading
from collections.abc import Callable


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Sleep until cancelled or until the timeout. Returns True if cancelled."""
        return self._event.wait(timeout)

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:  # one tool failing to react must not stop the others
                pass

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` when cancelled (at once, if it already is). Returns a function that removes it."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return lambda: self._remove(callback)
        callback()
        return lambda: None

    def _remove(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)
