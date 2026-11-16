"""The sidebar and the session flows in the main window, against the fake backend (offscreen Qt)."""
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QLabel, QListWidget, QMessageBox  # noqa: E402

from etcode_desktop.config import Settings  # noqa: E402
from etcode_desktop.core.models import ModelInfo  # noqa: E402
from etcode_desktop.core.store import Scope, SessionStore  # noqa: E402
from etcode_desktop.ui import bridge as states  # noqa: E402
from etcode_desktop.ui import main_window as mw  # noqa: E402
from etcode_desktop.ui.bridge import BackendBridge  # noqa: E402
from etcode_desktop.ui.main_window import MainWindow  # noqa: E402
from etcode_desktop.ui.session_list import ID_ROLE  # noqa: E402


def wait_until(qapp, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return bool(condition())


class Env:
    """Builds windows (each with its own bridge) on one store file, and cleans up."""

    def __init__(self, qapp, fake, tmp_path):
        self.qapp, self.fake, self.path = qapp, fake, tmp_path / "store.sqlite3"
        self.stores, self.windows, self.bridges = [], [], []

    def settings(self, token="tok-a", url=None):
        return Settings(backend_url=url or self.fake.url, token=token)

    def store(self):
        store = SessionStore(self.path)
        self.stores.append(store)
        return store

    def open(self, token="tok-a", url=None, store=None, start=True, interval=3_600_000, tools=None, config=None, image_loader=None):
        store = store or self.store()
        settings = self.settings(token, url)
        bridge = BackendBridge(store, settings, refresh_interval_ms=interval)
        window = MainWindow(settings, store, bridge, tools=tools, config=config, image_loader=image_loader)
        window.show()
        self.windows.append(window)
        self.bridges.append(bridge)
        if start:
            bridge.start()
        return window, bridge, store

    def titles(self, window):
        sidebar = window.sidebar
        lst = sidebar.findChild(QListWidget, "sessionList")
        return [lst.item(i).text().split("\n")[0] for i in range(lst.count())]

    def close(self):
        for bridge in self.bridges:
            bridge.stop()
        for window in self.windows:
            window.close()
        for store in self.stores:
            store.close()
        for window in self.windows:  # delete the widgets now, on this thread, and not whenever the garbage collector comes
            window.deleteLater()
        for bridge in self.bridges:
            bridge.deleteLater()
        self.qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qapp.processEvents()


@pytest.fixture
def env(qapp, fake, tmp_path, monkeypatch):
    # a real message box would block the test: record the text instead
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda parent, title, text, *a, **k: shown.append(text)))
    e = Env(qapp, fake, tmp_path)
    e.errors = shown
    yield e
    e.close()


def connected(window):
    return window.findChild(QLabel, "stateLabel").text()


class StubDialog:
    """Stands in for NewSessionDialog: accepts at once with a preset choice."""
    choice = ("fake-coder", False)
    seen = []

    def __init__(self, models, last_model=None, parent=None, last_options=None):
        StubDialog.seen.append((list(models), last_model))

    DialogCode = QDialog.DialogCode

    def exec(self):
        return QDialog.DialogCode.Accepted

    def model_name(self):
        return StubDialog.choice[0]

    def websearch(self):
        return StubDialog.choice[1]

    def options(self):
        from etcode_desktop.core.options import SessionOptions
        return SessionOptions()


@pytest.fixture
def stub_dialog(monkeypatch):
    StubDialog.choice = ("fake-coder", False)
    StubDialog.seen = []
    monkeypatch.setattr(mw, "NewSessionDialog", StubDialog)
    return StubDialog


# --- the sidebar fills from the backend ---------------------------------------------------------------------------------

def test_sessions_of_the_backend_appear_newest_first_with_titles(env, qapp, fake):
    old, new = fake.add_session(), fake.add_session()
    client_session = fake.sessions[old.id]
    client_session.messages.append({"role": "user", "content": [{"type": "text", "text": "Fix the failing test"}]})
    client_session.updated_at = "2099-01-01T00:00:00+00:00"
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: len(env.titles(window)) == 2)
    assert env.titles(window) == ["Fix the failing test", "New session"]
    assert wait_until(qapp, lambda: "connected" in connected(window))
    assert window.sidebar.ids() == [old.id, new.id]


def test_the_state_label_follows_the_connection(env, qapp, fake):
    window, bridge, _ = env.open(start=False)
    assert "connecting" in connected(window)
    bridge.start()
    assert wait_until(qapp, lambda: "● connected" == connected(window))


def test_an_unreachable_backend_shows_the_stored_sessions_and_says_so(env, qapp, fake):
    fake.add_session()
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: env.titles(window) == ["New session"])
    window.close()
    dead, dead_bridge, _ = env.open(url="http://127.0.0.1:9")  # another URL is another scope: nothing to show
    assert wait_until(qapp, lambda: states.OFFLINE == dead_bridge.state)
    assert "unreachable" in connected(dead) and env.titles(dead) == []
    assert dead.findChild(QLabel, "stateLabel").toolTip()  # says why


def test_stored_sessions_are_shown_before_and_without_the_backend(env, qapp, fake):
    fake.add_session()
    window, _, store = env.open()
    assert wait_until(qapp, lambda: len(env.titles(window)) == 1)
    fake.stop()  # the backend goes away
    again, bridge, _ = env.open(store=store)
    assert env.titles(again) == ["New session"]  # at once, from the store
    assert wait_until(qapp, lambda: bridge.state == states.OFFLINE)
    assert env.titles(again) == ["New session"]  # and it stays


def test_a_rejected_token_is_reported(env, qapp, fake):
    window, bridge, _ = env.open(token="wrong")
    assert wait_until(qapp, lambda: bridge.state == states.UNAUTHORIZED)
    assert "token rejected" in connected(window)
    assert "rejected" in window.findChild(QLabel, "stateLabel").toolTip()


def test_without_a_token_nothing_is_requested_and_new_is_disabled(env, qapp, fake):
    window, bridge, _ = env.open(token="")
    assert wait_until(qapp, lambda: bridge.state == states.NO_TOKEN)
    assert "no token" in connected(window)
    assert not window.sidebar.findChild(type(window.sidebar._new_button), "newSessionButton").isEnabled()
    window.request_new_session()
    time.sleep(0.1)
    qapp.processEvents()
    assert fake.requests == []


def test_a_session_that_vanished_from_the_backend_is_greyed_out(env, qapp, fake):
    session = fake.add_session()
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: len(env.titles(window)) == 1)
    del fake.sessions[session.id]
    bridge.refresh()
    lst = window.sidebar.findChild(QListWidget, "sessionList")
    assert wait_until(qapp, lambda: "not on the backend" in lst.item(0).text())


def test_sessions_are_refreshed_periodically(env, qapp, fake):
    window, bridge, _ = env.open(interval=60)
    assert wait_until(qapp, lambda: len(fake.requests_to("GET", "^/sessions$")) >= 3)
    fake.add_session()
    assert wait_until(qapp, lambda: len(env.titles(window)) == 1)


def test_refreshes_requested_while_one_runs_are_merged(env, qapp, fake):
    window, bridge, _ = env.open(start=False)
    fake.fail_next("GET", "^/sessions$", delay=0.3, times=1)
    for _ in range(10):
        bridge.refresh()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    assert wait_until(qapp, lambda: len(fake.requests_to("GET", "^/sessions$")) == 2)
    time.sleep(0.2)
    assert len(fake.requests_to("GET", "^/sessions$")) == 2  # one run, plus exactly one more for the rest


# --- selecting --------------------------------------------------------------------------------------------------------------------

def test_selecting_a_session_shows_its_conversation_and_remembers_that_it_was_opened(env, qapp, fake):
    session = fake.add_session()
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "Hello <b>there</b>"}]})
    session.messages.append({"role": "assistant", "text": {"type": "text", "text": "Hi!"}, "reasoning": {"type": "text", "text": ""},
                             "tool_calls": [], "status": "complete", "error": None})
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: window.sidebar.ids() == [session.id])
    assert window.shown_session_id is None and window.findChild(QLabel, "placeholder").isVisibleTo(window)
    window.sidebar.select(session.id)
    assert window.shown_session_id == session.id
    assert window.pane.isVisibleTo(window) and not window.findChild(QLabel, "placeholder").isVisibleTo(window)
    assert window.pane.chat is window.chat_for_test(session.id)
    assert wait_until(qapp, lambda: window.pane.chat.state.loaded)
    window.pane.transcript.flush()
    blocks = window.pane.transcript.blocks_in_order()
    assert [type(b).__name__ for b in blocks] == ["UserBlock", "AssistantBlock"]
    assert blocks[0].body_text() == "Hello <b>there</b>"  # shown as typed: not interpreted
    assert blocks[1].body_text() == "Hi!"
    assert window.pane.status_text == "idle"
    assert store.get(bridge.scope, session.id).last_opened is not None


def test_selecting_nothing_goes_back_to_the_placeholder(env, qapp, fake):
    session = fake.add_session()
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: window.sidebar.ids() == [session.id])
    window.sidebar.select(session.id)
    window.sidebar.archive_requested.emit(session.id, True)
    assert window.shown_session_id is None
    assert window.findChild(QLabel, "placeholder").isVisibleTo(window) and window.pane.chat is None


def test_switching_between_sessions_keeps_both_conversations_alive(env, qapp, fake):
    first, second = fake.add_session(), fake.add_session()
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: len(window.sidebar.ids()) == 2)
    window.sidebar.select(first.id)
    chat_one = window.chat_for_test(first.id)
    window.sidebar.select(second.id)
    chat_two = window.chat_for_test(second.id)
    assert chat_one is not chat_two and window.pane.chat is chat_two
    window.sidebar.select(first.id)
    assert window.pane.chat is chat_one and window.chat_for_test(first.id) is chat_one  # not opened a second time
    assert wait_until(qapp, lambda: fake.requests_to("GET", f"/session/{first.id}/messages$"))
    time.sleep(0.1)
    assert len(fake.requests_to("GET", f"/session/{first.id}/messages$")) == 1


# --- new session --------------------------------------------------------------------------------------------------------------------

def test_new_session_creates_stores_and_selects_it(env, qapp, fake, stub_dialog):
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    stub_dialog.choice = ("fake-text", True)
    window.request_new_session()
    assert wait_until(qapp, lambda: window.shown_session_id is not None)
    created = window.shown_session_id
    assert fake.sessions[created].model == "fake-text" and fake.sessions[created].websearch is True
    record = store.get(bridge.scope, created)
    assert record is not None and record.model == "fake-text"
    assert window.sidebar.ids() == [created] and window.sidebar.selected_id() == created
    assert store.kv_get("last_model") == "fake-text"


def test_the_model_list_is_fetched_once_and_the_last_model_is_offered(env, qapp, fake, stub_dialog):
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    window.request_new_session()
    assert wait_until(qapp, lambda: len(fake.sessions) == 1)
    stub_dialog.choice = ("fake-text", False)
    window.request_new_session()
    assert wait_until(qapp, lambda: len(fake.sessions) == 2)
    assert len(fake.requests_to("GET", "^/models$")) == 1
    assert [last for _, last in stub_dialog.seen] == [None, "fake-coder"]
    assert [m.tech_name for m in stub_dialog.seen[0][0]] == ["fake-coder", "fake-coder-lite", "fake-text"]


def test_the_session_exists_in_the_store_even_if_the_window_never_hears_about_it(env, qapp, fake, stub_dialog):
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    window.request_new_session()
    assert wait_until(qapp, lambda: len(fake.sessions) == 1)
    created = next(iter(fake.sessions))
    assert wait_until(qapp, lambda: store.get(bridge.scope, created) is not None)


def test_a_failing_model_list_shows_an_error_and_no_dialog(env, qapp, fake, stub_dialog):
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED and bridge.models is not None)  # fetched in the background
    bridge._models = None  # as if that had not worked
    fake.fail_next("GET", "^/models$", status=500, code="boom", message="it broke", times=1)
    window.request_new_session()
    assert wait_until(qapp, lambda: env.errors)
    assert "it broke" in env.errors[0] and stub_dialog.seen == []
    window.request_new_session()  # the user can try again
    assert wait_until(qapp, lambda: len(stub_dialog.seen) == 1)


def test_a_failing_creation_shows_the_error_and_stores_nothing(env, qapp, fake, stub_dialog):
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    stub_dialog.choice = ("not-a-model", False)
    window.request_new_session()
    assert wait_until(qapp, lambda: env.errors)
    assert "Unknown model" in env.errors[0]
    assert store.list(bridge.scope) == [] and fake.sessions == {}


def test_creating_with_a_dead_backend_reports_it(env, qapp, fake, stub_dialog):
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    window.request_new_session()
    assert wait_until(qapp, lambda: len(fake.sessions) == 1)
    fake.stop()
    window.request_new_session()
    assert wait_until(qapp, lambda: env.errors)
    assert bridge.state == states.OFFLINE and "unreachable" in connected(window)


def test_cancelling_the_dialog_creates_nothing(env, qapp, fake, monkeypatch):
    class Cancel(StubDialog):
        def exec(self):
            return QDialog.DialogCode.Rejected
    monkeypatch.setattr(mw, "NewSessionDialog", Cancel)
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    window.request_new_session()
    assert wait_until(qapp, lambda: bridge.models is not None)
    time.sleep(0.1)
    qapp.processEvents()
    assert fake.sessions == {}


def test_the_new_button_opens_the_dialog(env, qapp, fake, stub_dialog):
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    window.sidebar._new_button.click()
    assert wait_until(qapp, lambda: len(fake.sessions) == 1)


# --- adding a session by id ------------------------------------------------------------------------------------------------------------

def test_a_session_can_be_added_by_its_id(env, qapp, fake, monkeypatch):
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    other = fake.add_session()  # made elsewhere, after the last refresh
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: (f"  {other.id} ", True)))
    window.request_attach()
    assert wait_until(qapp, lambda: window.shown_session_id == other.id)
    assert store.get(bridge.scope, other.id) is not None


def test_adding_an_unknown_id_shows_an_error(env, qapp, fake, monkeypatch):
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("0" * 32, True)))
    window.request_attach()
    assert wait_until(qapp, lambda: env.errors)
    assert "not found" in env.errors[0].lower()
    assert bridge.state == states.CONNECTED  # a missing session says nothing about the connection


def test_cancelling_the_attach_dialog_does_nothing(env, qapp, fake, monkeypatch):
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED and bridge.models is not None)
    before = len(fake.requests)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("abc", False)))
    window.request_attach()
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("   ", True)))
    window.request_attach()
    time.sleep(0.1)
    qapp.processEvents()
    assert len(fake.requests) == before


# --- rename, remove ----------------------------------------------------------------------------------------------------------------------------

def test_rename_and_back_to_the_automatic_title(env, qapp, fake, monkeypatch):
    session = fake.add_session()
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "server title"}]})
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: env.titles(window) == ["server title"])
    window.sidebar.select(session.id)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("My own name", True)))
    window.sidebar.rename(session.id)
    assert env.titles(window) == ["My own name"]
    bridge.refresh()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    time.sleep(0.1)
    qapp.processEvents()
    assert env.titles(window) == ["My own name"]  # a refresh doesn't undo it
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", True)))
    window.sidebar.rename(session.id)
    bridge.refresh()
    assert wait_until(qapp, lambda: env.titles(window) == ["server title"])


def test_rename_dialog_is_prefilled_with_a_custom_title_only(env, qapp, fake, monkeypatch):
    session = fake.add_session()
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: window.sidebar.ids() == [session.id])
    seen = []
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: seen.append(k.get("text")) or ("", False)))
    window.sidebar.rename(session.id)
    store.set_title(bridge.scope, session.id, "mine")
    window.reload_sessions()
    window.sidebar.rename(session.id)
    assert seen == ["", "mine"]


def test_remove_from_list_and_show_removed(env, qapp, fake):
    keep, drop = fake.add_session(), fake.add_session()
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: len(window.sidebar.ids()) == 2)
    window.sidebar.select(drop.id)
    window.sidebar.archive_requested.emit(drop.id, True)
    assert window.sidebar.ids() == [keep.id]
    assert window.shown_session_id is None  # the shown session left the list
    bridge.refresh()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    time.sleep(0.1)
    qapp.processEvents()
    assert window.sidebar.ids() == [keep.id]  # the backend still lists it, but it stays removed
    window.sidebar.set_show_archived(True)
    assert set(window.sidebar.ids()) == {keep.id, drop.id}
    assert store.kv_get("show_archived_sessions") is True
    window.sidebar.archive_requested.emit(drop.id, False)
    window.sidebar.set_show_archived(False)
    assert set(window.sidebar.ids()) == {keep.id, drop.id}


def test_the_show_removed_choice_is_remembered(env, qapp, fake):
    window, bridge, store = env.open(start=False)
    window.sidebar.set_show_archived(True)
    window.close()
    again, _, _ = env.open(store=store, start=False)
    assert again.sidebar.findChild(type(again.sidebar._show_archived), "showArchived").isChecked()


def test_the_context_menu_has_the_right_entries(env, qapp, fake):
    session = fake.add_session()
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: window.sidebar.ids() == [session.id])
    menu = window.sidebar._menu_for(session.id)
    names = {a.objectName(): a for a in menu.actions() if a.objectName()}
    assert set(names) == {"renameAction", "copyIdAction", "archiveAction"}
    assert names["archiveAction"].text() == "Remove from list"
    requested = []
    window.sidebar.archive_requested.connect(lambda i, a: requested.append((i, a)))
    names["archiveAction"].trigger()
    assert requested == [(session.id, True)]
    names["copyIdAction"].trigger()
    assert QApplication.clipboard().text() == session.id
    store.set_archived(bridge.scope, session.id, True)
    window.sidebar.set_show_archived(True)
    again = {a.objectName(): a for a in window.sidebar._menu_for(session.id).actions() if a.objectName()}
    assert again["archiveAction"].text() == "Show in the list again"


# --- scopes and persistence ------------------------------------------------------------------------------------------------------------------------

def test_switching_the_token_shows_the_other_tokens_sessions(env, qapp, fake):
    mine = fake.add_session(owner="tok-a")
    theirs = fake.add_session(owner="tok-b")
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: window.sidebar.ids() == [mine.id])
    window.sidebar.select(mine.id)
    bridge.reconfigure(env.settings(token="tok-b"))
    assert window.sidebar.ids() == []  # at once: nothing of the old token is shown
    assert window.shown_session_id is None
    assert wait_until(qapp, lambda: window.sidebar.ids() == [theirs.id])
    bridge.reconfigure(env.settings(token="tok-a"))
    assert wait_until(qapp, lambda: window.sidebar.ids() == [mine.id])
    assert store.ids(Scope.for_token(fake.url, "tok-b")) == {theirs.id}  # kept, just not shown


def test_reconfigure_with_the_same_backend_does_nothing(env, qapp, fake):
    window, bridge, _ = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED and bridge.models is not None)  # the catalog is fetched at once
    before = len(fake.requests)
    bridge.reconfigure(Settings(backend_url=fake.url, token="tok-a", max_parallel_tools=3))
    time.sleep(0.1)
    qapp.processEvents()
    assert len(fake.requests) == before and bridge.settings.max_parallel_tools == 3


def test_answers_for_an_old_scope_are_dropped(env, qapp, fake):
    window, bridge, _ = env.open(start=False)
    called = []
    old_epoch = bridge._epoch
    bridge.reconfigure(env.settings(token="tok-b"))
    bridge._on_finished(old_epoch, lambda value: called.append(value), "stale", None)
    bridge._on_finished(bridge._epoch, lambda value: called.append(value), "fresh", None)
    assert called == ["fresh"]


def test_nothing_happens_after_stop(env, qapp, fake):
    window, bridge, _ = env.open(start=False)
    bridge.stop()
    bridge.refresh()
    time.sleep(0.1)
    qapp.processEvents()
    assert fake.requests == []


def test_the_window_layout_is_remembered(env, qapp, fake):
    window, bridge, store = env.open(start=False)
    window.resize(900, 600)
    window._splitter.setSizes([333, 500])
    qapp.processEvents()
    sizes = window._splitter.sizes()
    assert sizes[0] != window.sidebar.minimumWidth() and sizes[0] != 280  # not the defaults
    window.close()
    assert isinstance(store.kv_get("window_geometry"), str) and store.kv_get("splitter_sizes") == sizes
    again, _, _ = env.open(store=store, start=False)
    qapp.processEvents()
    # the offscreen screen is only 800 pixels wide, so the restored window can be narrower: compare the sidebar
    assert abs(again._splitter.sizes()[0] - sizes[0]) <= 20  # scaled a little if the restored window is narrower


def test_a_broken_saved_layout_is_ignored(env, qapp, fake):
    store = env.store()
    store.kv_set("window_geometry", 12345)
    store.kv_set("splitter_sizes", ["a", "b", "c"])
    window, _, _ = env.open(store=store, start=False)
    assert window.isVisible()


def test_sessions_that_exist_only_in_the_store_survive_a_restart(env, qapp, fake, stub_dialog):
    window, bridge, store = env.open()
    assert wait_until(qapp, lambda: bridge.state == states.CONNECTED)
    window.request_new_session()
    assert wait_until(qapp, lambda: window.shown_session_id is not None)
    created = window.shown_session_id
    window.close()
    bridge.stop()
    store.close()
    reopened = SessionStore(env.path)
    env.stores.append(reopened)
    again, bridge2, _ = env.open(store=reopened, start=False)
    assert again.sidebar.ids() == [created]  # from the file, without any network


# --- the dialog ----------------------------------------------------------------------------------------------------------------------------------------

def models():
    return [
        ModelInfo("Coder", "coder", 1000, True, "arena", True),
        ModelInfo("Text", "text", 1000, False, "openrouter", True),
        ModelInfo("Offline", "offline", 1000, False, "openrouter", False),
    ]


def test_dialog_lists_models_and_preselects_the_last_one(qapp):
    from etcode_desktop.ui.new_session_dialog import NewSessionDialog
    dialog = NewSessionDialog(models(), last_model="text")
    assert dialog.model_name() == "text" and dialog.websearch() is False
    assert NewSessionDialog(models(), last_model="nope").model_name() == "coder"
    assert NewSessionDialog(models(), last_model="offline").model_name() == "coder"  # unavailable: not preselected


def test_dialog_marks_unavailable_models_and_cannot_choose_them(qapp):
    from etcode_desktop.ui.new_session_dialog import NewSessionDialog
    dialog = NewSessionDialog(models())
    combo = dialog._combo
    assert "images" in combo.itemText(0) and "images" not in combo.itemText(1)
    assert "not available" in combo.itemText(2)
    assert not combo.model().item(2).flags() & Qt.ItemFlag.ItemIsEnabled
    combo.setCurrentIndex(2)
    assert dialog.model_name() is None


def test_dialog_without_usable_models_cannot_be_accepted(qapp):
    from etcode_desktop.ui.new_session_dialog import NewSessionDialog
    from PySide6.QtWidgets import QDialogButtonBox
    for given in ([], [models()[2]]):
        dialog = NewSessionDialog(given)
        assert not dialog._buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        assert dialog.model_name() is None
        assert dialog._notice.text() and not dialog._notice.isHidden()


def test_dialog_web_search_checkbox(qapp):
    from etcode_desktop.ui.new_session_dialog import NewSessionDialog
    dialog = NewSessionDialog(models())
    dialog._web.setChecked(True)
    assert dialog.websearch() is True


# --- the list widget on its own ------------------------------------------------------------------------------------------------------------------------------

def make_record(i, **kw):
    from etcode_desktop.core.store import SessionRecord
    base = dict(id=f"id{i}", backend_url="u", token_fp="t", title=f"t{i}", title_custom=False, model="m", status="idle",
                cost_total=None, created_at=1.0, updated_at=float(i), last_opened=None, last_seen_index=None, archived=False)
    base.update(kw)
    return SessionRecord(**base)


def test_set_records_keeps_the_selection_and_reports_a_vanished_one(qapp):
    from etcode_desktop.ui.session_list import SessionListWidget
    widget = SessionListWidget()
    changes = []
    widget.session_selected.connect(changes.append)
    widget.set_records([make_record(3), make_record(2), make_record(1)])
    assert widget.ids() == ["id3", "id2", "id1"] and widget.selected_id() is None
    widget.select("id2")
    assert changes == ["id2"]
    widget.set_records([make_record(4), make_record(3), make_record(2), make_record(1)])
    assert widget.selected_id() == "id2" and changes == ["id2"]  # a reload is not a selection change
    widget.set_records([make_record(4)])
    assert widget.selected_id() is None and changes == ["id2", None]
    assert not widget.select("unknown")


def test_empty_list_shows_a_hint(qapp):
    from etcode_desktop.ui.session_list import SessionListWidget
    widget = SessionListWidget()
    widget.show()
    widget.set_records([])
    assert widget._empty.isVisible() and not widget._list.isVisible()
    widget.set_records([make_record(1)])
    assert not widget._empty.isVisible() and widget._list.isVisible()


def test_items_are_marked_for_missing_and_removed_sessions(qapp):
    from etcode_desktop.ui.session_list import SessionListWidget
    widget = SessionListWidget()
    widget.set_records([make_record(1, status="missing"), make_record(2, archived=True), make_record(3)], now=100.0)
    texts = [widget._list.item(i).text() for i in range(3)]
    assert "not on the backend" in texts[0] and "removed" in texts[1]
    assert widget._list.item(0).font().italic() and widget._list.item(1).font().italic() and not widget._list.item(2).font().italic()
    assert widget._list.item(0).data(ID_ROLE) == "id1"
    assert "Id: id3" in widget._list.item(2).toolTip()


# --- error texts and states --------------------------------------------------------------------------------------------------------------------------------

def test_error_descriptions_and_their_connection_states():
    from etcode_desktop.core.errors import (
        AuthError, BackendError, Conflict, NotFound, ProtocolError, ServerError, Unreachable,
    )
    from etcode_desktop.core.store import StoreError
    cases = [
        (AuthError(401, "unauthorized", "Invalid bearer token"), states.UNAUTHORIZED, "rejected the access token"),
        (Unreachable("cannot connect to http://x"), states.OFFLINE, "cannot connect to http://x"),
        (ServerError(503, "busy", "try later"), states.OFFLINE, "try later (busy)"),
        (ProtocolError("not json"), states.OFFLINE, "Unexpected reply"),
        (NotFound(404, "session_not_found", "Session not found"), None, "Session not found (session_not_found)"),
        (Conflict(409, "session_busy", "busy now"), None, "busy now (session_busy)"),
        (BackendError(400, "unknown_model", "Unknown model"), None, "Unknown model (unknown_model)"),
        (StoreError("disk full"), None, "disk full"),
        (ValueError("a session id is required"), None, "a session id is required"),
        (RuntimeError("odd"), None, "RuntimeError: odd"),
    ]
    for error, state, text in cases:
        assert states.state_for_error(error) == state, error
        assert text in states.describe_error(error), error
