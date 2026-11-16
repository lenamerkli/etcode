"""The connection settings dialog (offscreen Qt)."""
import json
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPushButton  # noqa: E402

from etcode_desktop.config import DEFAULT_BACKEND_URL, ConfigStore  # noqa: E402
from etcode_desktop.core.errors import AuthError, Unreachable  # noqa: E402
from etcode_desktop.ui.connection_dialog import ConnectionDialog, check_connection  # noqa: E402


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(0o600)


def pump(qapp, condition, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


@pytest.fixture
def config(paths):
    return ConfigStore(paths, environ={})


def make(config, tester=None, environ=None):
    store = ConfigStore(config.paths, environ=environ if environ is not None else {})
    settings = store.load()
    return ConnectionDialog(settings, store, tester or (lambda url, token: "Connected."), None), store


def save_button(dialog):
    return dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save)


def click_save(dialog):
    save_button(dialog).click()


# --- what it shows ---------------------------------------------------------------------------------------------------------

def test_the_fields_start_with_what_is_in_use_and_say_where_it_comes_from(qapp, config, paths):
    write(paths.provisioned_file, {"backend_url": "http://10.0.2.2:9000", "token": "prov-token"})
    dialog, _ = make(config)
    assert dialog.url_edit.text() == "http://10.0.2.2:9000" and dialog.token_edit.text() == "prov-token"
    assert dialog.token_edit.echoMode() == dialog.token_edit.EchoMode.Password  # hidden
    assert dialog.note_texts() == ("Provided by the manager.", "Provided by the manager.")
    assert not dialog.reset_enabled()  # there is nothing to reset


def test_without_anything_configured(qapp, config):
    dialog, _ = make(config)
    assert dialog.url_edit.text() == DEFAULT_BACKEND_URL and dialog.token_edit.text() == ""
    assert dialog.note_texts() == ("Not set: the default is used.", "No token is configured.")


def test_overrides_are_marked_and_can_be_reset(qapp, config, paths):
    write(paths.provisioned_file, {"backend_url": "http://prov:1", "token": "prov-token"})
    write(paths.user_config_file, {"token": "my-token"})
    dialog, _ = make(config)
    assert dialog.token_edit.text() == "my-token"
    assert dialog.note_texts()[1] == "Set in this window. 'Reset' goes back to what the manager provided."
    assert dialog.reset_enabled()


def test_a_value_from_the_environment_is_marked_as_unchangeable(qapp, config):
    dialog, _ = make(config, environ={"ETCODE_API_TOKEN": "env-token", "ETCODE_BACKEND_URL": "http://env:1"})
    url_note, token_note = dialog.note_texts()
    assert "ETCODE_BACKEND_URL" in url_note and "no effect" in url_note
    assert "ETCODE_API_TOKEN" in token_note and "no effect" in token_note


def test_the_token_can_be_shown(qapp, config):
    dialog, _ = make(config)
    dialog.show()
    box = dialog.findChild(type(dialog._show), "showToken")
    box.setChecked(True)
    assert dialog.token_edit.echoMode() == dialog.token_edit.EchoMode.Normal
    box.setChecked(False)
    assert dialog.token_edit.echoMode() == dialog.token_edit.EchoMode.Password


def test_http_beyond_the_private_network_is_warned_about(qapp, config):
    dialog, _ = make(config)
    dialog.url_edit.setText("http://example.com")
    assert "clear text" in dialog.warning_text
    dialog.url_edit.setText("https://example.com")
    assert dialog.warning_text == ""
    dialog.url_edit.setText("http://10.0.2.2:8765")
    assert dialog.warning_text == ""


# --- saving -----------------------------------------------------------------------------------------------------------------

def test_a_changed_token_is_saved_as_an_override_and_the_rest_is_not(qapp, config, paths):
    write(paths.provisioned_file, {"backend_url": "http://prov:1", "token": "prov-token"})
    dialog, store = make(config)
    dialog.token_edit.setText("  new-token\n")
    click_save(dialog)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert json.loads(paths.user_config_file.read_text()) == {"token": "new-token"}  # the URL still is the manager's
    settings = store.load()
    assert settings.token == "new-token" and settings.backend_url == "http://prov:1"


def test_values_equal_to_the_provisioned_ones_are_not_stored(qapp, config, paths):
    write(paths.provisioned_file, {"backend_url": "http://prov:1", "token": "prov-token"})
    write(paths.user_config_file, {"token": "old-override", "backend_url": "http://other:2"})
    dialog, store = make(config)
    dialog.url_edit.setText("http://prov:1/")  # the same address again
    dialog.token_edit.setText("prov-token")
    click_save(dialog)
    assert json.loads(paths.user_config_file.read_text()) == {}  # the overrides are gone
    assert store.load().token == "prov-token"


def test_reset_puts_the_provisioned_values_in_the_fields_and_saving_removes_the_overrides(qapp, config, paths):
    write(paths.provisioned_file, {"backend_url": "http://prov:1", "token": "prov-token"})
    write(paths.user_config_file, {"token": "mine", "backend_url": "http://mine:2"})
    dialog, store = make(config)
    assert dialog.url_edit.text() == "http://mine:2"
    dialog.findChild(QPushButton, "resetButton").click()
    assert dialog.url_edit.text() == "http://prov:1" and dialog.token_edit.text() == "prov-token"
    assert json.loads(paths.user_config_file.read_text()) == {"token": "mine", "backend_url": "http://mine:2"}  # nothing saved yet
    click_save(dialog)
    assert store.load().token == "prov-token" and store.override_values() == {}


def test_reset_without_provisioned_values_goes_to_the_default_and_no_token(qapp, config, paths):
    write(paths.user_config_file, {"token": "mine", "backend_url": "http://mine:2"})
    dialog, store = make(config)
    dialog.findChild(QPushButton, "resetButton").click()
    assert dialog.url_edit.text() == DEFAULT_BACKEND_URL and dialog.token_edit.text() == ""
    click_save(dialog)
    settings = store.load()
    assert settings.backend_url == DEFAULT_BACKEND_URL and not settings.has_token


def test_an_empty_address_means_the_default(qapp, config):
    dialog, store = make(config)
    dialog.url_edit.setText("   ")
    dialog.token_edit.setText("some-token")
    click_save(dialog)
    assert store.load().backend_url == DEFAULT_BACKEND_URL


@pytest.mark.parametrize("url, token, message", [
    ("ftp://host", "tok", "must start with http:// or https://"),
    ("http://user:pw@host", "tok", "not in the URL"),
    ("http://host", "bad token", "not allowed in an HTTP header"),
    ("http://host:99999", "tok", "invalid backend URL"),
])
def test_wrong_input_is_explained_and_nothing_is_saved(qapp, config, paths, url, token, message):
    dialog, _ = make(config)
    dialog.url_edit.setText(url)
    dialog.token_edit.setText(token)
    click_save(dialog)
    assert message in dialog.error_text
    assert dialog.result() != QDialog.DialogCode.Accepted and not paths.user_config_file.exists()


def test_a_settings_file_that_cannot_be_written_is_reported(qapp, config, paths, monkeypatch):
    dialog, store = make(config)
    dialog.token_edit.setText("new-token")

    def refuse(updates):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "save_overrides", refuse)
    click_save(dialog)
    assert "could not be saved" in dialog.error_text and dialog.result() != QDialog.DialogCode.Accepted


def test_cancel_changes_nothing(qapp, config, paths):
    dialog, _ = make(config)
    dialog.token_edit.setText("typed but not saved")
    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
    assert dialog.result() == QDialog.DialogCode.Rejected and not paths.user_config_file.exists()


# --- testing the connection -----------------------------------------------------------------------------------------------------

def test_a_successful_test_says_so_and_saves_nothing(qapp, config, paths):
    seen = []
    dialog, _ = make(config, tester=lambda url, token: seen.append((url, token)) or "Connected: 3 models.")
    dialog.url_edit.setText("http://10.0.2.2:1234/")
    dialog.token_edit.setText(" typed-token ")
    dialog.findChild(QPushButton, "testButton").click()
    assert dialog.result_text == "Testing…"
    assert pump(qapp, lambda: dialog.result_text.startswith("✔"))
    assert dialog.result_text == "✔ Connected: 3 models." and seen == [("http://10.0.2.2:1234", "typed-token")]
    assert dialog.findChild(QPushButton, "testButton").isEnabled() and not paths.user_config_file.exists()


@pytest.mark.parametrize("error, expected", [
    (AuthError(401, "unauthorized", "Invalid bearer token"), "rejected the access token"),
    (Unreachable("cannot connect to http://10.0.2.2:8765"), "cannot connect to http://10.0.2.2:8765"),
])
def test_a_failed_test_says_why(qapp, config, error, expected):
    def failing(url, token):
        raise error

    dialog, _ = make(config, tester=failing)
    dialog.token_edit.setText("tok")
    dialog.findChild(QPushButton, "testButton").click()
    assert pump(qapp, lambda: dialog.result_text.startswith("✘"))
    assert expected in dialog.result_text and dialog.findChild(QPushButton, "testButton").isEnabled()


def test_a_bug_in_the_test_is_shown_not_swallowed(qapp, config):
    def broken(url, token):
        raise RuntimeError("something odd")

    dialog, _ = make(config, tester=broken)
    dialog.token_edit.setText("tok")
    dialog.findChild(QPushButton, "testButton").click()
    assert pump(qapp, lambda: "RuntimeError: something odd" in dialog.result_text)
    assert dialog.findChild(QPushButton, "testButton").isEnabled()


def test_testing_without_a_token_or_with_a_bad_address_does_not_start(qapp, config):
    called = []
    dialog, _ = make(config, tester=lambda url, token: called.append(1) or "ok")
    dialog.findChild(QPushButton, "testButton").click()
    assert dialog.error_text == "There is no token to test." and called == []
    dialog.token_edit.setText("tok")
    dialog.url_edit.setText("nonsense")
    dialog.findChild(QPushButton, "testButton").click()
    assert "http://" in dialog.error_text and called == []


def test_an_old_test_does_not_overwrite_a_newer_one(qapp, config):
    release = []

    def slow_then_fast(url, token):
        if not release:
            release.append(1)
            time.sleep(0.4)
            return "the first, slow test"
        return "the second test"

    dialog, _ = make(config, tester=slow_then_fast)
    dialog.token_edit.setText("tok")
    button = dialog.findChild(QPushButton, "testButton")
    button.click()
    button.setEnabled(True)
    button.click()
    assert pump(qapp, lambda: "second" in dialog.result_text)
    time.sleep(0.6)
    qapp.processEvents()
    assert dialog.result_text == "✔ the second test"


def test_the_real_check_asks_the_backend_for_its_models(qapp, fake):
    pytest.importorskip("flask")
    text = check_connection(fake.url, "tok-a")
    assert text == "Connected: the backend offers 3 models, 3 usable."
    fake.unavailable.add("fake-text")
    assert check_connection(fake.url, "tok-a") == "Connected: the backend offers 3 models, 2 usable."
    with pytest.raises(AuthError):
        check_connection(fake.url, "wrong")
    with pytest.raises(Unreachable):
        check_connection("http://127.0.0.1:9", "tok-a")
