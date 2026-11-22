"""Menus, shortcuts, the connection settings in the window, the window title (offscreen Qt, fake backend)."""
import json
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox, QPushButton, QToolButton  # noqa: E402

from etcode_desktop.config import ConfigStore  # noqa: E402
from etcode_desktop.core.errors import Unreachable  # noqa: E402
from etcode_desktop.paths import Paths  # noqa: E402
from etcode_desktop.ui import bridge as states  # noqa: E402
from etcode_desktop.ui import main_window as mw  # noqa: E402
from etcode_desktop.ui.main_window import SHORTCUTS  # noqa: E402
from test_chat_pane import open_session, type_and_send  # noqa: E402
from test_gui_sessions import Env, wait_until  # noqa: E402


@pytest.fixture
def env(qapp, fake, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda parent, title, text, *a, **k: shown.append(text)))
    e = Env(qapp, fake, tmp_path)
    e.messages = shown
    fake.auto_reply = True
    yield e
    e.close()


@pytest.fixture
def config(tmp_path, fake):
    """Settings files below tmp_path, with the fake backend as what the manager provided. (The conftest 'env' fixture is
    replaced in this module, so 'paths' cannot be used.)"""
    paths = Paths(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data", state_dir=tmp_path / "state", runtime_dir=tmp_path / "run",
                  provisioned_file=tmp_path / "provisioned.json")
    paths.ensure_dirs()
    paths.provisioned_file.write_text(json.dumps({"backend_url": fake.url, "token": "tok-a"}))
    return ConfigStore(paths, environ={})


def action(window, name):
    found = window.findChild(QAction, name)
    assert found is not None, name
    return found


class AcceptingDialog:
    """Stands in for ConnectionDialog: saves a new token, as if the user typed it, and accepts."""
    DialogCode = QDialog.DialogCode
    instances = []

    def __init__(self, settings, config, tester=None, parent=None):
        self.settings, self.config = settings, config
        self.token, self.accept = "new-token", True
        AcceptingDialog.instances.append(self)

    def exec(self):
        if self.accept:
            self.config.save_overrides({"token": self.token})
            return QDialog.DialogCode.Accepted
        return QDialog.DialogCode.Rejected


@pytest.fixture
def accepting(monkeypatch):
    AcceptingDialog.instances = []
    monkeypatch.setattr(mw, "ConnectionDialog", AcceptingDialog)
    return AcceptingDialog


# --- the menus and the shortcuts ----------------------------------------------------------------------------------------------

def test_the_menus_have_the_actions_with_their_shortcuts(env, qapp, fake):
    window, bridge, _ = env.open()
    titles = [a.text() for a in window.menuBar().actions()]
    assert titles == ["&Session", "&View", "&Help"]
    expected = {"newSessionAction": "Ctrl+N", "connectionSettingsAction": "Ctrl+,", "quitAction": "Ctrl+Q", "focusSessionsAction": "Ctrl+K",
                "focusMessageAction": "Ctrl+L", "jumpToLatestAction": "Ctrl+End"}
    for name, keys in expected.items():
        assert action(window, name).shortcut().toString() == keys, name
    for name in ("attachSessionAction", "shortcutsAction", "aboutAction"):
        assert action(window, name).text()


def test_every_shortcut_in_the_help_window_exists(env, qapp, fake):
    window, bridge, _ = env.open()
    assigned = {a.shortcut().toString() for a in window.findChildren(QAction) if not a.shortcut().isEmpty()}
    for keys, what in SHORTCUTS:
        if "+" in keys and keys != "Ctrl+Enter":  # handled directly by the message box
            assert keys in assigned, f"{keys} ({what}) is listed but nothing has it"
    assert len({keys for keys, _ in SHORTCUTS}) == len(SHORTCUTS)


def test_ctrl_k_goes_to_the_session_list_and_ctrl_l_to_the_message_box(env, qapp, fake):
    session = fake.add_session()
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: window.sidebar.ids() == [session.id])
    window.activateWindow()
    action(window, "focusSessionsAction").trigger()
    assert window.sidebar.selected_id() == session.id  # the first one, so that the arrow keys have a place to start
    assert window.shown_session_id == session.id
    action(window, "focusMessageAction").trigger()  # (whether a widget really has the focus depends on the windowing system)
    assert wait_until(qapp, lambda: window.pane.chat is not None and window.pane.chat.state.loaded)


def test_ctrl_n_as_a_key_opens_a_new_session(env, qapp, fake, monkeypatch):
    from test_gui_sessions import StubDialog
    StubDialog.choice, StubDialog.seen = ("fake-coder", False), []
    monkeypatch.setattr(mw, "NewSessionDialog", StubDialog)
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    window.show()
    window.activateWindow()
    window.sidebar.focus_list()  # a shortcut is looked up from the widget that has the focus
    if not wait_until(qapp, lambda: qapp.activeWindow() is window and qapp.focusWidget() is not None and window.isAncestorOf(qapp.focusWidget()), 3):
        pytest.skip("the offscreen platform did not give this window the focus, so a window shortcut cannot fire")
    QTest.keyClick(window, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
    assert wait_until(qapp, lambda: len(fake.sessions) == 1, 5)


def test_jump_to_latest_scrolls_down(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    pane.transcript.verticalScrollBar().setValue(0)
    action(window, "jumpToLatestAction").trigger()
    assert pane.transcript.at_bottom


def test_the_help_windows(env, qapp, fake, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda parent, title, text, *a, **k: shown.append((title, text))))
    monkeypatch.setattr(QMessageBox, "about", staticmethod(lambda parent, title, text: shown.append((title, text))))
    window, bridge, _ = env.open()
    action(window, "shortcutsAction").trigger()
    action(window, "aboutAction").trigger()
    assert shown[0][0] == "Keyboard shortcuts" and all(keys in shown[0][1] and what in shown[0][1] for keys, what in SHORTCUTS)
    assert shown[1][0] == "About etcode" and "etcode 0.1.0" in shown[1][1]


def test_quit_closes_the_window(env, qapp, fake):
    window, bridge, _ = env.open()
    action(window, "quitAction").trigger()
    assert not window.isVisible()


# --- the window title -----------------------------------------------------------------------------------------------------------

def test_the_title_names_the_session_that_is_shown(env, qapp, fake):
    first, second = fake.add_session(), fake.add_session()
    first.messages.append({"role": "user", "content": [{"type": "text", "text": "Fix the build"}]})
    window, bridge, store = env.open()
    assert window.windowTitle() == "etcode"
    assert wait_until(qapp, lambda: len(window.sidebar.ids()) == 2)
    window.sidebar.select(first.id)
    assert window.windowTitle() == "etcode: Fix the build"
    window.sidebar.select(second.id)
    assert window.windowTitle() == "etcode"  # this one has no title yet
    store.set_title(bridge.scope, second.id, "x" * 100)
    window.reload_sessions()
    assert window.windowTitle() == "etcode: " + "x" * 60
    window.sidebar.archive_requested.emit(second.id, True)
    assert window.windowTitle() == "etcode"


def test_the_title_follows_the_first_message(env, qapp, fake):
    window, pane, session, store, bridge = open_session(env, qapp, fake)
    type_and_send(pane, "Please explain")
    assert wait_until(qapp, lambda: window.windowTitle() == "etcode: Please explain")


# --- connection settings in the window ----------------------------------------------------------------------------------------------

def test_the_gear_and_the_menu_open_the_connection_settings(env, qapp, fake, config, accepting):
    window, bridge, _ = env.open(config=config)
    window.findChild(QToolButton, "gearButton").click()
    action(window, "connectionSettingsAction").trigger()
    assert len(accepting.instances) == 2


def test_without_a_config_the_settings_cannot_be_opened(env, qapp, fake, accepting):
    window, bridge, _ = env.open()
    assert not window.findChild(QToolButton, "gearButton").isEnabled() and not action(window, "connectionSettingsAction").isEnabled()
    assert window.open_connection_settings() is False and accepting.instances == []


def test_saving_a_new_token_switches_to_that_tokens_sessions(env, qapp, fake, config, accepting):
    fake.tokens.add("new-token")
    mine = fake.add_session(owner="tok-a")
    theirs = fake.add_session(owner="new-token")
    window, bridge, store = env.open(config=config)
    assert wait_until(qapp, lambda: window.sidebar.ids() == [mine.id])
    assert window.open_connection_settings() is True
    assert window.settings.token == "new-token" and bridge.settings.token == "new-token"
    assert wait_until(qapp, lambda: window.sidebar.ids() == [theirs.id])
    assert json.loads(config.paths.user_config_file.read_text()) == {"token": "new-token"}
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    assert "set in the GUI" in window.findChild(QLabel, "connectionLabel").text()


def test_cancelling_the_dialog_changes_nothing(env, qapp, fake, config, accepting):
    window, bridge, _ = env.open(config=config)
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)

    class Cancelling(AcceptingDialog):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.accept = False

    mw.ConnectionDialog = Cancelling
    try:
        assert window.open_connection_settings() is False
    finally:
        mw.ConnectionDialog = AcceptingDialog
    assert window.settings.token == "tok-a" and not config.paths.user_config_file.exists()


def test_the_new_token_is_kept_out_of_the_log(env, qapp, fake, config, accepting):
    from etcode_desktop.logging_setup import clear_secrets, redact
    window, bridge, _ = env.open(config=config)
    clear_secrets()  # forget what was registered so far: the next line must come from saving the new token
    assert redact("the token new-token leaked") == "the token new-token leaked"
    window.open_connection_settings()
    assert redact("the token new-token leaked") == "the token *** leaked"
    clear_secrets()


def test_a_vm_without_a_token_asks_for_one_at_the_start(env, qapp, fake, config, accepting):
    window, bridge, _ = env.open(token="", config=config)
    assert window.prompt_for_missing_token() is True
    assert len(accepting.instances) == 1 and window.settings.token == "new-token"
    assert window.prompt_for_missing_token() is False  # now there is one


def test_a_vm_with_a_token_is_not_asked(env, qapp, fake, config, accepting):
    window, bridge, _ = env.open(config=config)
    assert window.prompt_for_missing_token() is False and accepting.instances == []


def test_the_settings_button_appears_when_the_token_is_missing_or_refused(env, qapp, fake, config, accepting):
    window, bridge, _ = env.open(token="", config=config)
    assert wait_until(qapp, lambda: bridge.state == states.NO_TOKEN)
    button = window.findChild(QPushButton, "connectionSettingsButton")
    assert not button.isHidden()
    button.click()
    assert len(accepting.instances) == 1
    fake.tokens.discard("new-token")  # the new token is not accepted by this backend
    assert wait_until(qapp, lambda: bridge.state == states.UNAUTHORIZED, 8)
    assert not button.isHidden()


def test_a_refused_token_offers_the_settings_once(env, qapp, fake, config, accepting, monkeypatch):
    questions = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda parent, title, text, *a, **k: questions.append(text) or QMessageBox.StandardButton.No))
    fake.tokens.discard("tok-a")
    window, bridge, _ = env.open(config=config, interval=60_000)
    assert wait_until(qapp, lambda: bridge.state == states.UNAUTHORIZED and questions)
    assert "rejected the access token" in questions[0]
    bridge.refresh()
    time.sleep(0.3)
    qapp.processEvents()
    assert len(questions) == 1  # not again and again
    fake.tokens.add("tok-a")
    bridge.refresh()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    fake.tokens.discard("tok-a")
    bridge.refresh()
    assert wait_until(qapp, lambda: len(questions) == 2)  # a new refusal is offered again


def test_answering_yes_opens_the_settings(env, qapp, fake, config, accepting, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    fake.tokens.discard("tok-a")
    window, bridge, _ = env.open(config=config)
    assert wait_until(qapp, lambda: len(accepting.instances) == 1)


# --- the model catalog --------------------------------------------------------------------------------------------------------------

def test_the_catalog_is_fetched_as_soon_as_the_backend_answers_and_only_once(env, qapp, fake):
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: bridge.models is not None)
    window.request_new_session = lambda: None
    time.sleep(0.2)
    qapp.processEvents()
    assert len(fake.requests_to("GET", "^/models$")) == 1


def test_a_catalog_that_nobody_asked_for_fails_quietly(env, qapp, fake):
    fake.fail_next("GET", "^/models$", status=500, code="boom", message="it broke", times=1)  # the background fetch fails
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    assert wait_until(qapp, lambda: fake.requests_to("GET", "^/models$"))
    time.sleep(0.3)
    qapp.processEvents()
    assert env.messages == [] and bridge.models is None, (env.messages, bridge.state, [(r.method, r.path) for r in fake.requests])  # no pop-up


def test_a_user_request_during_a_quiet_fetch_makes_its_failure_visible(env, qapp, fake):
    fake.fail_next("GET", "^/models$", status=500, code="boom", message="it broke", times=1, delay=0.4)
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED and fake.requests_to("GET", "^/models$"))
    bridge.load_models()  # the user asks while the background fetch is on its way
    assert wait_until(qapp, lambda: env.messages, 5)
    assert "it broke" in env.messages[0]


def test_an_unreachable_backend_found_by_the_quiet_fetch_is_shown_in_the_status_bar(env, qapp, fake):
    window, bridge, _ = env.open(start=False)
    def gone():
        raise Unreachable("gone")

    bridge._client.models = gone
    bridge.load_models(quiet=True)
    assert wait_until(qapp, lambda: bridge.state == states.OFFLINE)
    assert env.messages == []
