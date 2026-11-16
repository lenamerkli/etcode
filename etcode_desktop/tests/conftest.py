import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A private home: all app files go below tmp_path, and no ETCODE_* variable leaks in."""
    for name in list(os.environ):
        if name.startswith("ETCODE_"):
            monkeypatch.delenv(name)
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "run")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ETCODE_PROVISIONED_FILE", str(tmp_path / "etc" / "desktop.json"))
    return tmp_path


@pytest.fixture
def paths(env):
    from etcode_desktop.paths import Paths
    result = Paths.discover()
    result.ensure_dirs()
    return result


@pytest.fixture(scope="session")
def qapp():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# --- backend fakes (need Flask, a dev dependency) -------------------------------------------

@pytest.fixture
def fake():
    pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")
    from fake_backend import FakeBackend
    backend = FakeBackend(tokens=("tok-a", "tok-b"))
    backend.start()
    yield backend
    backend.stop()


@pytest.fixture
def legacy_fake():
    pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")
    from fake_backend import FakeBackend
    backend = FakeBackend(tokens=("tok-a",), legacy=True)
    backend.start()
    yield backend
    backend.stop()


@pytest.fixture
def make_client():
    """Build clients with fast timeouts and no sleeping between retries. They are closed afterwards."""
    from etcode_desktop.core.api import BackendClient, RetryPolicy, Timeouts
    created = []

    def factory(backend_or_url, token="tok-a", **kwargs):
        url = getattr(backend_or_url, "url", backend_or_url)
        kwargs.setdefault("timeouts", Timeouts(connect=2.0, read=5.0, upload_read=5.0))
        kwargs.setdefault("retry", RetryPolicy(attempts=3, base_delay=0.0, max_delay=0.0))
        kwargs.setdefault("sleep", lambda seconds: None)
        client = BackendClient(url, token, **kwargs)
        created.append(client)
        return client

    yield factory
    for client in created:
        client.close()


@pytest.fixture(autouse=True)
def close_stray_windows():
    """A window or dialog that a test leaves open takes the focus and the active-window state from the tests after it
    (which breaks window shortcuts, for one). So everything that is still open is closed and deleted after each test."""
    yield
    if "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        try:
            if widget.isVisible():
                widget.close()
            widget.deleteLater()
        except RuntimeError:  # already deleted
            pass
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
