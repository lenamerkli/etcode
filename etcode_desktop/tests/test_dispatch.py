import gc
import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject  # noqa: E402

from etcode_desktop.ui import dispatch  # noqa: E402
from etcode_desktop.ui.dispatch import GuiDispatcher, dispatcher  # noqa: E402


def pump(qapp, seconds=0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.004)


def test_a_posted_callback_runs_on_the_gui_thread(qapp):
    seen = []
    gui = threading.current_thread()
    d = GuiDispatcher(tick_ms=5)
    worker = threading.Thread(target=lambda: d.post(lambda x: seen.append((x, threading.current_thread())), 42))
    worker.start()
    worker.join()
    assert seen == []  # not yet: it needs the GUI thread
    pump(qapp)
    assert seen == [(42, gui)]


def test_callbacks_run_in_the_order_they_were_posted_across_threads(qapp):
    d = GuiDispatcher(tick_ms=5)
    seen = []
    for i in range(50):
        d.post(seen.append, i)
    threads = [threading.Thread(target=lambda n=n: [d.post(seen.append, 1000 * n + j) for j in range(20)]) for n in range(1, 4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pump(qapp, 0.4)
    assert seen[:50] == list(range(50))
    for n in range(1, 4):
        assert [x for x in seen if x // 1000 == n] == [1000 * n + j for j in range(20)]  # each thread's own order is kept


def test_a_failing_callback_does_not_stop_the_others_or_the_dispatcher(qapp, caplog):
    d = GuiDispatcher(tick_ms=5)
    seen = []
    d.post(lambda: 1 / 0)
    d.post(seen.append, "after")
    pump(qapp)
    d.post(seen.append, "much later")
    pump(qapp)
    assert seen == ["after", "much later"] and "a callback from a background thread failed" in caplog.text


def test_a_callback_for_a_deleted_object_is_dropped_quietly(qapp, caplog):
    d = GuiDispatcher(tick_ms=5)
    victim = QObject()
    method = victim.objectName
    import shiboken6
    shiboken6.delete(victim)
    seen = []
    d.post(method)
    d.post(seen.append, "next")
    pump(qapp)
    assert seen == ["next"] and "failed" not in caplog.text


def test_a_flood_does_not_freeze_the_window(qapp):
    d = GuiDispatcher(tick_ms=5)
    done = []
    for i in range(5000):
        d.post(done.append, i)
    started = time.monotonic()
    qapp.processEvents()
    time.sleep(0.01)
    qapp.processEvents()
    assert len(done) < 5000 and time.monotonic() - started < 1  # a batch per tick, the rest later
    pump(qapp, 1.5)
    assert len(done) == 5000


def test_flush_runs_everything_at_once(qapp):
    d = GuiDispatcher(tick_ms=1000)
    done = []
    for i in range(1200):
        d.post(done.append, i)
    d.flush()
    assert len(done) == 1200


def test_posting_from_threads_while_the_owner_is_deleted_cannot_crash(qapp):
    d = GuiDispatcher(tick_ms=5)
    stop = threading.Event()
    posted = []
    post = d.post  # what the threads hold on to: the method, as the controller's callbacks do

    def spam():
        while not stop.is_set():
            post(posted.append, 1)
            time.sleep(0.0005)

    threads = [threading.Thread(target=spam) for _ in range(4)]
    for t in threads:
        t.start()
    pump(qapp, 0.1)
    d.deleteLater()
    del d
    gc.collect()
    pump(qapp, 0.2)  # the dispatcher is gone: the threads go on posting into a queue that nobody reads, and nothing breaks
    stop.set()
    for t in threads:
        t.join()
    assert posted


def test_the_shared_dispatcher_is_one_object_and_is_replaced_if_it_died(qapp):
    first = dispatcher()
    assert dispatcher() is first
    import shiboken6
    shiboken6.delete(first)
    second = dispatcher()
    assert second is not first and dispatch._alive(second)
    seen = []
    second.post(seen.append, 1)
    pump(qapp)
    assert seen == [1]
