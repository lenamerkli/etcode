import threading
import time

import pytest

from etcode_desktop.core.tools import ToolContext
from etcode_desktop.core.tools.cancel import CancelToken
from etcode_desktop.core.tools.scheduler import BatchCall, ToolPool, ToolRunner


def call(call_id, name="bash", **arguments):
    return BatchCall(call_id, name, arguments)


class Recorder:
    def __init__(self, pause=0.0, gate=None):
        self.events, self.lock, self.pause, self.gate = [], threading.Lock(), pause, gate
        self.running = self.peak = 0

    def __call__(self, c):
        with self.lock:
            self.events.append(("start", c.id))
            self.running += 1
            self.peak = max(self.peak, self.running)
        if self.gate:
            self.gate.wait(5)
        time.sleep(self.pause)
        with self.lock:
            self.running -= 1
            self.events.append(("end", c.id))

    def order(self, kind):
        return [i for k, i in self.events if k == kind]


def run_batch(runner, calls, work):
    futures = runner.submit_batch(calls, work)
    for future in futures:
        future.result(10)


# --- the pool ---------------------------------------------------------------------------------------------------------------------

def test_the_pool_runs_jobs_side_by_side_up_to_its_size():
    pool = ToolPool(3)
    recorder = Recorder(pause=0.2)
    futures = [pool.submit(recorder, call(f"c{i}")) for i in range(9)]
    started = time.monotonic()
    for f in futures:
        f.result(10)
    assert recorder.peak == 3 and pool.thread_count == 3
    assert 0.5 < time.monotonic() - started < 3  # three rounds of 0.2 s


def test_the_pool_only_starts_threads_that_it_needs():
    pool = ToolPool(8)
    for _ in range(5):
        pool.submit(lambda: None).result(5)
        time.sleep(0.02)
    assert pool.thread_count <= 2


def test_the_pool_returns_results_and_exceptions_through_the_future():
    pool = ToolPool(2)
    assert pool.submit(lambda a, b: a + b, 2, 3).result(5) == 5

    def boom():
        raise ValueError("no")

    with pytest.raises(ValueError):
        pool.submit(boom).result(5)
    assert pool.submit(lambda: "still works").result(5) == "still works"


def test_pool_threads_are_daemons_so_quitting_never_waits_for_a_tool():
    pool = ToolPool(2)
    gate = threading.Event()
    pool.submit(gate.wait, 30)
    time.sleep(0.05)
    assert all(t.daemon for t in pool._threads)
    gate.set()


def test_a_shut_down_pool_refuses_work_and_lets_workers_go():
    pool = ToolPool(2)
    pool.submit(lambda: None).result(5)
    pool.shutdown()
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)
    time.sleep(0.1)
    assert not any(t.is_alive() for t in pool._threads)


def test_a_pool_of_zero_still_has_one_worker():
    assert ToolPool(0).max_workers == 1


# --- a batch ------------------------------------------------------------------------------------------------------------------------

def test_calls_of_a_batch_run_one_after_another_in_order():
    runner = ToolRunner(ToolPool(8))
    recorder = Recorder()
    run_batch(runner, [call(f"c{i}") for i in range(5)], recorder)
    assert recorder.order("start") == ["c0", "c1", "c2", "c3", "c4"]
    assert recorder.order("end") == ["c0", "c1", "c2", "c3", "c4"]
    assert recorder.peak == 1


def test_every_kind_of_call_takes_its_turn():
    runner = ToolRunner(ToolPool(8), home=None)
    recorder = Recorder(pause=0.05)
    calls = [call("w", "write_to_file", path="/tmp/same.txt"), call("b", "bash", command="cat /tmp/same.txt"),
             call("r", "read_file", path="/tmp/same.txt"), call("v", "view_image", path="/tmp/same.txt"),
             call("b2", "bash", command="echo done")]
    run_batch(runner, calls, recorder)
    assert recorder.order("start") == ["w", "b", "r", "v", "b2"]
    assert recorder.order("end") == ["w", "b", "r", "v", "b2"]
    assert recorder.peak == 1


def test_a_failing_call_stops_the_calls_after_it():
    runner = ToolRunner(ToolPool(4))
    done = []

    def work(c):
        if c.id == "bad":
            raise RuntimeError("fails")
        done.append(c.id)

    futures = runner.submit_batch([call("bad", "write_to_file", path="/tmp/x.txt"), call("good", "read_file", path="/tmp/x.txt"),
                                   call("other", "bash", command="x")], work)
    with pytest.raises(RuntimeError):
        futures[0].result(5)
    assert futures[1].result(5) is None and futures[2].result(5) is None
    assert done == []


def test_a_batch_larger_than_the_pool_still_finishes_in_order():
    runner = ToolRunner(ToolPool(2))
    recorder = Recorder(pause=0.02)
    calls = [call(f"f{i}", "write_to_file", path="/tmp/chain.txt") for i in range(6)] + [call(f"b{i}") for i in range(6)]
    run_batch(runner, calls, recorder)  # must not deadlock
    assert recorder.order("start") == [f"f{i}" for i in range(6)] + [f"b{i}" for i in range(6)]
    assert recorder.peak == 1


def test_a_work_function_that_returns_false_stops_the_rest_of_the_batch():
    runner = ToolRunner(ToolPool(2))
    seen = []

    def work(c):
        seen.append(c.id)
        return False

    futures = runner.submit_batch([call("c1"), call("c2"), call("c3")], work)
    assert futures[0].result(5) is False and futures[1].result(5) is None and futures[2].result(5) is None
    assert seen == ["c1"]


def test_stop_skips_the_rest_of_the_batch():
    runner = ToolRunner(ToolPool(2))
    seen = []
    stopped = threading.Event()
    def work(c):
        seen.append(c.id)
        stopped.set()

    futures = runner.submit_batch([call("c1"), call("c2")], work, stop=stopped.is_set)
    assert futures[0].result(5) is None
    assert futures[1].result(5) is None
    assert seen == ["c1"]


# --- run -----------------------------------------------------------------------------------------------------------------------------

def test_run_hands_the_context_to_the_executor():
    seen = {}

    def execute(name, arguments, context):
        seen.update(name=name, arguments=arguments, context=context)
        return [{"type": "text", "text": "ok"}]

    cancel, client = CancelToken(), object()
    runner = ToolRunner(execute=execute)
    assert runner.run("bash", {"command": "x"}, client, cancel) == [{"type": "text", "text": "ok"}]
    assert isinstance(seen["context"], ToolContext) and seen["context"].client is client and seen["context"].cancel is cancel
    assert seen["context"].home == runner.home


# --- the cancel token --------------------------------------------------------------------------------------------------------------

def test_cancel_token_basics():
    token = CancelToken()
    assert not token.cancelled and token.wait(0.01) is False
    called = []
    token.on_cancel(lambda: called.append("a"))
    remove = token.on_cancel(lambda: called.append("b"))
    remove()
    token.cancel()
    token.cancel()  # twice is fine, and calls nothing again
    assert token.cancelled and token.wait(0) is True and called == ["a"]


def test_a_callback_added_after_cancelling_runs_at_once():
    token = CancelToken()
    token.cancel()
    called = []
    token.on_cancel(lambda: called.append(1))
    assert called == [1]


def test_a_failing_callback_does_not_stop_the_others():
    token = CancelToken()
    called = []
    token.on_cancel(lambda: 1 / 0)
    token.on_cancel(lambda: called.append("second"))
    token.cancel()
    assert called == ["second"]


def test_wait_wakes_up_when_cancelled():
    token = CancelToken()
    threading.Timer(0.1, token.cancel).start()
    started = time.monotonic()
    assert token.wait(5) is True and time.monotonic() - started < 2
