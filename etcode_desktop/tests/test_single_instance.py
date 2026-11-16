import pytest

pytest.importorskip("PySide6")

from etcode_desktop.ui.single_instance import SingleInstance, default_server_name  # noqa: E402


def pump(app, predicate, timeout_s=3.0):
    import time
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def lock_path(env):
    return env / "run" / "etcode_desktop.lock"


def test_second_instance_is_refused_and_raises_the_first(qapp, lock_path):
    first = SingleInstance(lock_path)
    second = SingleInstance(lock_path)
    raised = []
    first.raise_requested.connect(lambda: raised.append(1))
    try:
        assert first.acquire() is True
        assert second.acquire(retries=3) is False
        assert pump(qapp, lambda: raised), "the first instance was not asked to raise its window"
    finally:
        second.release()
        first.release()


def test_lock_is_free_after_release(qapp, lock_path):
    first = SingleInstance(lock_path)
    assert first.acquire()
    first.release()
    again = SingleInstance(lock_path)
    try:
        assert again.acquire() is True
    finally:
        again.release()


def test_a_stale_socket_does_not_prevent_startup(qapp, lock_path):
    crashed = SingleInstance(lock_path)
    assert crashed.acquire()
    crashed._lock.unlock()  # simulate a crash: lock gone, socket left behind
    crashed._lock = None
    fresh = SingleInstance(lock_path)
    try:
        assert fresh.acquire() is True
    finally:
        fresh.release()
        crashed.release()


def test_refused_when_the_holder_does_not_answer(qapp, lock_path):
    # The lock is held but nobody listens (a hung or half-started instance).
    holder = SingleInstance(lock_path, server_name="etcode-test-holder")
    holder.acquire()
    holder._server.close()
    other = SingleInstance(lock_path, server_name="etcode-test-holder")
    try:
        assert other.acquire(retries=2, retry_delay_ms=20) is False
    finally:
        holder.release()


def test_server_name_is_stable_and_short(lock_path, tmp_path):
    assert default_server_name(lock_path) == default_server_name(lock_path)
    assert default_server_name(lock_path) != default_server_name(tmp_path / "other.lock")
    assert len(default_server_name(lock_path)) < 40


def test_instances_can_be_deleted_after_a_raise_without_crashing(tmp_path):
    # Regression: a lambda that held the server side socket made the process crash a little after the
    # instances were deleted. A crash cannot be caught inside pytest, so this runs in a process of its own.
    import subprocess
    import sys
    from pathlib import Path
    code = f"""
import gc, os, sys, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from pathlib import Path
from PySide6.QtWidgets import QApplication
app = QApplication([])
from etcode_desktop.ui.single_instance import SingleInstance
lock = Path({str(tmp_path)!r}) / "x.lock"
first, second = SingleInstance(lock), SingleInstance(lock)
raised = []
first.raise_requested.connect(lambda: raised.append(1))
assert first.acquire() and second.acquire(retries=3) is False
deadline = time.monotonic() + 3
while not raised and time.monotonic() < deadline:
    app.processEvents(); time.sleep(0.01)
assert raised
second.release(); first.release()
del first, second
gc.collect()
for _ in range(10):
    app.processEvents()
print("ok")
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "ok" in result.stdout, (result.returncode, result.stderr)
