"""The connection settings: the backend's address and the access token.

The fields start with what is in use now. On **Save**, a value that is the same as what the manager provided (or the
default) is not stored as an override, so that "reset to provisioned values" really means "no overrides". A value that an
environment variable sets cannot be changed here, and the dialog says so.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..config import (
    DEFAULT_BACKEND_URL, ENV_TOKEN, ENV_URL, KEY_TOKEN, KEY_URL, SOURCE_DEFAULT, SOURCE_ENV, SOURCE_GUI, SOURCE_PROVISIONED,
    ConfigStore, Settings, is_insecure_url, normalize_token, normalize_url,
)
from ..core.api import BackendClient
from ..core.errors import EtcodeError
from ..core.describe import describe_error
from .dispatch import dispatcher

log = logging.getLogger(__name__)

SOURCE_TEXT = {
    SOURCE_ENV: "from an environment variable",
    SOURCE_GUI: "set in this window",
    SOURCE_PROVISIONED: "provided by the manager",
    SOURCE_DEFAULT: "the default",
}
_ENV_NAME = {KEY_URL: ENV_URL, KEY_TOKEN: ENV_TOKEN}


def check_connection(url: str, token: str) -> str:
    """Ask the backend for its models. Returns a sentence for the user, or raises an EtcodeError."""
    client = BackendClient(url, token)
    try:
        models = client.models()
    finally:
        client.close()
    usable = sum(1 for m in models if m.available)
    return f"Connected: the backend offers {len(models)} model{'s' if len(models) != 1 else ''}, {usable} usable."



class ConnectionDialog(QDialog):
    def __init__(self, settings: Settings, config: ConfigStore, tester: Callable[[str, str], str] = check_connection,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Connection settings")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._settings = settings
        self._config = config
        self._tester = tester
        self._dispatch = dispatcher()
        self._test_serial = 0
        self._provisioned = config.provisioned_values()
        self._overrides = config.override_values()
        self._env_keys = config.env_keys()

        self._url = QLineEdit(settings.backend_url)
        self._url.setObjectName("urlEdit")
        self._url.setPlaceholderText(DEFAULT_BACKEND_URL)
        self._token = QLineEdit(settings.token)
        self._token.setObjectName("tokenEdit")
        self._token.setEchoMode(QLineEdit.EchoMode.Password)
        self._token.setPlaceholderText("the access token for the backend")
        self._show = QCheckBox("Show the token")
        self._show.setObjectName("showToken")
        self._show.toggled.connect(lambda shown: self._token.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password))
        self._url_note = self._note("urlNote")
        self._token_note = self._note("tokenNote")
        self._warning = QLabel()
        self._warning.setObjectName("insecureWarning")
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet("color: #8a5a00;")
        self._error = QLabel()
        self._error.setObjectName("formError")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #8a1f1f;")
        self._result = QLabel()
        self._result.setObjectName("testResult")
        self._result.setWordWrap(True)

        self._test = QPushButton("Test connection")
        self._test.setObjectName("testButton")
        self._test.clicked.connect(self.start_test)
        self._reset = QPushButton("Reset to provisioned values")
        self._reset.setObjectName("resetButton")
        self._reset.setToolTip("Forget what was set in this window, and use what the manager provided")
        self._reset.clicked.connect(self.reset_to_provisioned)
        self._reset.setEnabled(bool(self._overrides))
        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._save)
        self._buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Backend address", self._url)
        form.addRow("", self._url_note)
        form.addRow("Access token", self._token)
        form.addRow("", self._show)
        form.addRow("", self._token_note)
        tools = QHBoxLayout()
        tools.addWidget(self._test)
        tools.addWidget(self._reset)
        tools.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._warning)
        layout.addLayout(tools)
        layout.addWidget(self._result)
        layout.addWidget(self._error)
        layout.addWidget(self._buttons)

        self._url.textChanged.connect(self._refresh)
        self._token.textChanged.connect(self._refresh)
        self._refresh()

    @staticmethod
    def _note(name: str) -> QLabel:
        label = QLabel()
        label.setObjectName(name)
        label.setWordWrap(True)
        label.setStyleSheet("color: #6b7280; font-size: 11px;")
        return label

    # --- what the dialog says -------------------------------------------------------------------------------------------

    def _source_sentence(self, key: str) -> str:
        if key in self._env_keys:
            return (f"This comes from the environment variable {_ENV_NAME[key]}. A change made here is saved, but it has no effect "
                    "while that variable is set.")
        if key in self._overrides:
            return "Set in this window. 'Reset' goes back to " + ("what the manager provided." if key in self._provisioned else "the default.")
        if key in self._provisioned:
            return "Provided by the manager."
        return "Not set: the default is used." if key == KEY_URL else "No token is configured."

    def _refresh(self) -> None:
        self._url_note.setText(self._source_sentence(KEY_URL))
        self._token_note.setText(self._source_sentence(KEY_TOKEN))
        url = self._url.text().strip()
        self._warning.setText("This address uses http:// over a network that is not private: the token would travel in clear text. "
                              "Use https://." if url and is_insecure_url(url) else "")
        self._error.setText("")

    # --- the values --------------------------------------------------------------------------------------------------------

    def _values(self) -> tuple[str, str | None]:
        """The URL and the token as typed, checked. Raises ValueError with a message for the user."""
        url_text = self._url.text().strip() or DEFAULT_BACKEND_URL
        return normalize_url(url_text), normalize_token(self._token.text())

    def _baseline(self, key: str) -> object:
        """What applies without any override from this window: what the manager provided, else the default."""
        if key == KEY_URL:
            return self._provisioned.get(KEY_URL, DEFAULT_BACKEND_URL)
        return self._provisioned.get(KEY_TOKEN)

    def overrides_to_save(self) -> dict[str, object | None]:
        """What to store: a value that differs from the baseline, or None to remove the override."""
        url, token = self._values()
        return {KEY_URL: None if url == self._baseline(KEY_URL) else url,
                KEY_TOKEN: None if token == self._baseline(KEY_TOKEN) else token}

    def reset_to_provisioned(self) -> None:
        """Put the fields back to what applies without overrides. Nothing is saved until Save."""
        self._url.setText(str(self._baseline(KEY_URL)))
        self._token.setText(str(self._baseline(KEY_TOKEN) or ""))

    def _save(self) -> None:
        try:
            updates = self.overrides_to_save()
        except ValueError as exc:
            self._error.setText(str(exc))
            return
        try:
            self._config.save_overrides(updates)
        except (OSError, ValueError) as exc:
            self._error.setText(f"The settings could not be saved: {exc}")
            return
        self.accept()

    # --- testing -------------------------------------------------------------------------------------------------------------

    def start_test(self) -> None:
        """Try the address and token as they are typed, without saving them. The result shows below."""
        try:
            url, token = self._values()
        except ValueError as exc:
            self._error.setText(str(exc))
            return
        if not token:
            self._result.setText("")
            self._error.setText("There is no token to test.")
            return
        self._test_serial += 1
        serial = self._test_serial
        self._test.setEnabled(False)
        self._result.setStyleSheet("")
        self._result.setText("Testing…")

        def work() -> None:
            try:
                message, ok = self._tester(url, token), True
            except EtcodeError as exc:
                message, ok = describe_error(exc), False
            except Exception as exc:  # whatever it is, the user should see it, not a frozen button
                log.exception("testing the connection failed")
                message, ok = f"{type(exc).__name__}: {exc}", False
            self._dispatch.post(self._finish_test, serial, message, ok)

        threading.Thread(target=work, daemon=True, name="connection-test").start()

    def _finish_test(self, serial: int, message: str, ok: bool) -> None:
        if serial != self._test_serial:
            return  # an older test: the fields have been tested again since
        self._test.setEnabled(True)
        self._result.setStyleSheet("color: #1f7a1f;" if ok else "color: #8a1f1f;")
        self._result.setText(("✔ " if ok else "✘ ") + message)

    # for tests
    @property
    def url_edit(self) -> QLineEdit:
        return self._url

    @property
    def token_edit(self) -> QLineEdit:
        return self._token

    @property
    def result_text(self) -> str:
        return self._result.text()

    @property
    def error_text(self) -> str:
        return self._error.text()

    @property
    def warning_text(self) -> str:
        return self._warning.text()

    def note_texts(self) -> tuple[str, str]:
        return self._url_note.text(), self._token_note.text()

    def reset_enabled(self) -> bool:
        return self._reset.isEnabled()

