"""Run the tool calls of one assistant message one after another, in the order the model gave them.

* :class:`ToolPool` is a small thread pool of **daemon** threads (a ``ThreadPoolExecutor`` would make the
  app wait for running tools when it quits). One submitted job is one whole tool round, so at most
  ``max_workers`` sessions run tools at the same time.
* :class:`ToolRunner` runs a batch serially on one pool thread. The next call starts only after the
  previous call's ``work`` returned - which, in the controller, means after its result was journaled
  and posted (or after the backend stored a backend-run tool's result). That is what makes
  ``write_to_file`` followed by ``bash``/``read_file`` in one message safe.
* If ``work`` raises, the remaining calls are not started. The controller retries from the journal,
  which resends the failed result first and then continues with the calls that never ran.
  ``stop`` (Stop/close) is checked before every call, so queued calls are skipped.
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ToolContext, execute_tool
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
    backend: bool = False  # the backend runs this call; the client only asks it to


class ToolRunner:
    def __init__(self, pool: ToolPool | None = None, *, execute: Callable[[str, dict[str, Any], ToolContext], list[dict[str, Any]]] = execute_tool,
                 home: Path | None = None):
        self.pool = pool or ToolPool()
        self.home = home or Path.home()
        self._execute = execute

    def run(self, name: str, arguments: dict[str, Any], client: Any, cancel: CancelToken) -> list[dict[str, Any]]:
        """Run one call now, on this thread. Always returns content parts, even if the executor itself breaks."""
        try:
            return self._execute(name, arguments, ToolContext(client=client, cancel=cancel, home=self.home))
        except Exception as exc:
            log.exception("running the tool %s failed", name)
            return [{"type": "text", "text": f"Error: the tool {name} failed: {type(exc).__name__}: {exc}"}]

    def submit_batch(self, calls: Sequence[BatchCall], work: Callable[[BatchCall], Any],
                     *, stop: Callable[[], bool] = lambda: False) -> list[Future]:
        """Run ``work(call)`` for every call **one after another**, in the order given, on one pool thread.

        One :class:`Future` per call is returned so that the controller can keep its ``_inflight``
        bookkeeping. They complete in call order. If a call raises, that call's future carries the
        exception, the remaining futures complete with ``None``, and no further call is started.
        If ``work`` returns exactly ``False``, the rest of the batch is skipped as well (used when
        another controller has claimed an earlier call: running a later one would break the order).
        """
        futures: list[Future] = [Future() for _ in calls]
        if calls:
            self.pool.submit(self._run_in_order, tuple(calls), tuple(futures), work, stop)
        return futures

    @staticmethod
    def _run_in_order(calls: Sequence[BatchCall], futures: Sequence[Future], work: Callable[[BatchCall], Any],
                      stop: Callable[[], bool]) -> None:
        for index, (call, future) in enumerate(zip(calls, futures)):
            if stop():
                _finish(future, None)
                continue
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = work(call)
                future.set_result(result)
            except BaseException as exc:
                future.set_exception(exc)
                for rest in futures[index + 1:]:
                    _finish(rest, None)
                return
            if result is False:  # an earlier call belongs to another controller: keep the order
                for rest in futures[index + 1:]:
                    _finish(rest, None)
                return


def _finish(future: Future, value: Any) -> None:
    if not future.done():
        future.set_result(value)
