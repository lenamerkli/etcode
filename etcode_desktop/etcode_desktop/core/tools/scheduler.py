"""Run the tool calls of one assistant message in parallel, with a little order where it matters.

* :class:`ToolPool` is a small thread pool of **daemon** threads (a ``ThreadPoolExecutor`` would make the
  app wait for running tools when it quits).
* :class:`ToolRunner` starts a batch of calls on the pool, all at once. Calls that work on the **same file**
  (``read_file``, ``write_to_file``, ``replace_in_file``) run one after the other in the order the model
  gave them, so that "write the file, then read it back" in one message works. Calls on different files, and
  ``bash``, run side by side. ``bash`` is not analysed: what a command touches cannot be known.

Waiting for an earlier call can't deadlock the pool: calls are queued in order, so the call that is waited
for has always been started before the one that waits.
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ToolContext, execute_tool, path_key
from .cancel import CancelToken

log = logging.getLogger(__name__)

DEFAULT_WORKERS = 8


class ToolPool:
    def __init__(self, max_workers: int = DEFAULT_WORKERS):
        self.max_workers = max(1, max_workers)
        self._queue: queue.SimpleQueue[tuple[Future, Callable[[], Any]] | None] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._idle = 0
        self._closed = False

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future:
        future: Future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("the tool pool is shut down")
            self._queue.put((future, lambda: fn(*args)))
            if self._idle == 0 and len(self._threads) < self.max_workers:
                thread = threading.Thread(target=self._work, daemon=True, name=f"tool-worker-{len(self._threads) + 1}")
                self._threads.append(thread)
                thread.start()
            elif self._idle > 0:
                self._idle -= 1  # that worker is about to pick this job up
        return future

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, job = item
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(job())
                except BaseException as exc:
                    future.set_exception(exc)
            with self._lock:
                self._idle += 1

    def shutdown(self) -> None:
        """Let the workers go once their queue is empty. Running calls are not interrupted (cancel their tokens)."""
        with self._lock:
            self._closed = True
            for _ in self._threads:
                self._queue.put(None)

    @property
    def thread_count(self) -> int:
        with self._lock:
            return len(self._threads)


@dataclass(frozen=True)
class BatchCall:
    id: str
    name: str
    arguments: dict[str, Any]


class ToolRunner:
    def __init__(self, pool: ToolPool | None = None, *, execute: Callable[[str, dict[str, Any], ToolContext], list[dict[str, Any]]] = execute_tool,
                 home: Path | None = None):
        self.pool = pool or ToolPool()
        self.home = home or Path.home()
        self._execute = execute
        self._lock = threading.RLock()  # a call that is over already runs its done-callback at once, inside submit_batch
        self._tails: dict[str, Future] = {}

    def run(self, name: str, arguments: dict[str, Any], client: Any, cancel: CancelToken) -> list[dict[str, Any]]:
        """Run one call now, on this thread. Always returns content parts, even if the executor itself breaks."""
        try:
            return self._execute(name, arguments, ToolContext(client=client, cancel=cancel, home=self.home))
        except Exception as exc:
            log.exception("running the tool %s failed", name)
            return [{"type": "text", "text": f"Error: the tool {name} failed: {type(exc).__name__}: {exc}"}]

    def submit_batch(self, calls: Sequence[BatchCall], work: Callable[[BatchCall], None]) -> list[Future]:
        """Start ``work(call)`` for every call on the pool, all at once, but in order for calls on the same file."""
        futures: list[Future] = []
        for call in calls:
            key = path_key(call.name, call.arguments, self.home)
            with self._lock:
                previous = self._tails.get(key) if key is not None else None
                future = self.pool.submit(self._ordered, call, work, previous)
                if key is not None:
                    self._tails[key] = future
                    future.add_done_callback(lambda done, key=key: self._release(key, done))
            futures.append(future)
        return futures

    @staticmethod
    def _ordered(call: BatchCall, work: Callable[[BatchCall], None], previous: Future | None) -> None:
        if previous is not None:
            wait([previous])  # its outcome does not matter here, only that it is over
        work(call)

    def _release(self, key: str, future: Future) -> None:
        with self._lock:
            if self._tails.get(key) is future:
                del self._tails[key]
