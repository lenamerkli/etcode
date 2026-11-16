"""Hand work from background threads to the GUI thread without touching a Qt object from the wrong thread.

A Qt signal emitted by a Python thread works, but if the receiving object is deleted while a thread is in the
middle of emitting, the process can crash. Worker threads here only put a callable in a plain Python queue. A timer on
the GUI thread runs what is in the queue. The dispatcher lives as long as the application, so no thread ever
meets a deleted object, and a callback that has become pointless (its window is gone) just returns.
"""
from __future__ import annotations

import logging
import queue
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer

log = logging.getLogger(__name__)

TICK_MS = 15  # the delay that a result can have before the GUI sees it
BATCH = 400  # callbacks run per tick, so that a flood cannot freeze the window


class GuiDispatcher(QObject):
    def __init__(self, tick_ms: int = TICK_MS, parent: QObject | None = None):
        super().__init__(parent)
        self._queue: queue.SimpleQueue[tuple[Callable[..., Any], tuple[Any, ...]]] = queue.SimpleQueue()
        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        self._timer.timeout.connect(self._drain)
        self._timer.start()

    def post(self, callback: Callable[..., Any], *args: Any) -> None:
        """Run ``callback(*args)`` on the GUI thread soon. Safe to call from any thread, at any time."""
        self._queue.put((callback, args))

    def _drain(self) -> None:
        for _ in range(BATCH):
            try:
                callback, args = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                callback(*args)
            except RuntimeError:
                pass  # the object that the callback belongs to has been deleted: nobody is waiting for it
            except Exception:
                log.exception("a callback from a background thread failed")

    def flush(self) -> None:
        """Run everything that is queued now (for tests, and for a shutdown that wants to be tidy)."""
        while not self._queue.empty():
            self._drain()


_instance: GuiDispatcher | None = None


def dispatcher() -> GuiDispatcher:
    """The dispatcher of this application. The first call must come from the GUI thread, after QApplication exists."""
    global _instance
    from PySide6.QtWidgets import QApplication

    if _instance is None or not _alive(_instance):  # (a new one if the application was recreated, as tests do)
        _instance = GuiDispatcher(parent=QApplication.instance())
    return _instance


def _alive(obj: QObject) -> bool:
    try:
        obj.objectName()
        return True
    except RuntimeError:
        return False
