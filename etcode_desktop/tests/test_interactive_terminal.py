import pytest

from etcode_desktop.core.store import SessionStore
from etcode_desktop.core.tools.cancel import CancelToken
from etcode_desktop.core.tools.interactive_terminal import TerminalError, TerminalManager, parse_actions


def test_action_parser_handles_escaped_commas_and_validates_limits():
    actions = parse_actions('type("a,b\\\\c"),press("CTRL+C"),wait(0.25),print(color),')
    assert [a.name for a in actions] == ["type", "press", "wait", "print"]
    assert actions[0].value == "a,b\\c"
    assert actions[2].value == 0.25
    assert len(parse_actions("wait(300),wait(300)")) == 2
    for bad in ('wat()', 'type(not-json)', 'press("CTRL+MADEUP")', 'wait(301)', 'wait(300),wait(301)', 'print(blue)'):
        with pytest.raises(TerminalError):
            parse_actions(bad)


def test_terminal_persists_in_a_chat_and_renders_screen(tmp_path):
    store = SessionStore(tmp_path / "store.sqlite3")
    manager = TerminalManager(store)
    result = manager.run(chat_id="chat-a", backend_url="http://backend", terminal_id="term-1",
                         actions='type("printf \\\"terminal-ok\\\""),press("ENTER"),wait(0.2),print()',
                         height=None, width=None, cancel=CancelToken())
    assert "terminal-ok" in result
    # Resizing and reusing the same id operate on the already-running shell.
    result = manager.run(chat_id="chat-a", backend_url="http://backend", terminal_id="term-1",
                         actions='type("echo second-call"),press("ENTER"),wait(0.2),print()',
                         height=30, width=100, cancel=CancelToken())
    assert "second-call" in result
    manager.shutdown()
    store.close()


def test_cleaned_terminal_ids_are_persisted_per_chat_across_restart(tmp_path):
    path = tmp_path / "store.sqlite3"
    store = SessionStore(path)
    first = TerminalManager(store)
    first.run(chat_id="chat-a", backend_url="http://backend", terminal_id="same-id",
              actions='type("sleep 60"),press("ENTER"),wait(0.1),print()',
              height=None, width=None, cancel=CancelToken())
    first.shutdown()
    assert store.terminal_cleaned_ids("http://backend", "chat-a") == ["same-id"]
    store.close()

    store = SessionStore(path)
    restarted = TerminalManager(store)
    with pytest.raises(TerminalError, match="cleaned up"):
        restarted.run(chat_id="chat-a", backend_url="http://backend", terminal_id="same-id",
                      actions="print()", height=None, width=None, cancel=CancelToken())
    # The same name in another chat is a different terminal namespace.
    assert restarted.run(chat_id="chat-b", backend_url="http://backend", terminal_id="same-id",
                         actions='type("echo other-chat"),press("ENTER"),wait(0.15),print()',
                         height=None, width=None, cancel=CancelToken())
    restarted.shutdown()
    store.close()


def test_restart_converts_unclosed_active_records_into_tombstones(tmp_path):
    store = SessionStore(tmp_path / "store.sqlite3")
    assert store.terminal_start("http://backend", "chat", "old-terminal")
    store.close()
    store = SessionStore(tmp_path / "store.sqlite3")
    manager = TerminalManager(store)
    assert store.terminal_state("http://backend", "chat", "old-terminal") == "cleaned"
    with pytest.raises(TerminalError, match="cleaned up"):
        manager.run(chat_id="chat", backend_url="http://backend", terminal_id="old-terminal",
                    actions="print()", height=None, width=None, cancel=CancelToken())
    manager.shutdown()
    store.close()
