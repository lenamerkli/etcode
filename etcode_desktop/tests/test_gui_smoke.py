import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QLabel  # noqa: E402

from etcode_desktop.config import (  # noqa: E402
    KEY_TOKEN, SOURCE_ENV, SOURCE_GUI, SOURCE_PROVISIONED, Settings,
)
from etcode_desktop.ui.main_window import MainWindow, connection_summary  # noqa: E402


def settings(**kwargs):
    return Settings(backend_url=kwargs.pop("backend_url", "http://10.0.2.2:8765"), **kwargs)


def test_window_shows_and_has_title(qapp):
    window = MainWindow(settings())
    window.show()
    qapp.processEvents()
    assert window.windowTitle() == "etcode"
    assert window.isVisible()
    assert window.findChild(QLabel, "placeholder") is not None
    window.close()


def test_status_bar_shows_backend_and_token_state(qapp):
    window = MainWindow(settings(token="tok-12345", sources={KEY_TOKEN: SOURCE_PROVISIONED}))
    text = window.findChild(QLabel, "connectionLabel").text()
    assert "http://10.0.2.2:8765" in text and "provisioned by the manager" in text
    assert "tok-12345" not in text


def test_connection_summary_variants():
    assert "no token configured" in connection_summary(settings())
    assert "set in the GUI" in connection_summary(settings(token="t-1234", sources={KEY_TOKEN: SOURCE_GUI}))
    assert "from the environment" in connection_summary(settings(token="t-1234", sources={KEY_TOKEN: SOURCE_ENV}))


def test_bring_to_front_restores_a_minimized_window(qapp):
    window = MainWindow(settings())
    window.show()
    window.showMinimized()
    qapp.processEvents()
    window.bring_to_front()
    qapp.processEvents()
    assert window.isVisible() and not window.isMinimized()
    window.close()
