import logging
import stat
import sys

import pytest

from etcode_desktop import logging_setup
from etcode_desktop.logging_setup import (
    PACKAGE_LOGGER, RedactingFormatter, clear_secrets, configure_logging, install_excepthook, redact, register_secret,
)


@pytest.fixture(autouse=True)
def clean_logging():
    yield
    clear_secrets()
    logger = logging.getLogger(PACKAGE_LOGGER)
    for handler in list(logger.handlers):
        if getattr(handler, logging_setup._HANDLER_MARK, False):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


def our_handlers():
    # pytest may attach its own capture handlers to loggers, so count only ours
    return [h for h in logging.getLogger(PACKAGE_LOGGER).handlers if getattr(h, logging_setup._HANDLER_MARK, False)]


def test_writes_a_private_log_file(paths):
    configure_logging(paths, console=False)
    logging.getLogger("etcode_desktop.test").info("hello log")
    for handler in our_handlers():
        handler.flush()
    assert "hello log" in paths.log_file.read_text()
    assert stat.S_IMODE(paths.log_file.stat().st_mode) == 0o600


def test_configure_twice_does_not_duplicate_handlers(paths):
    configure_logging(paths, console=False)
    configure_logging(paths, console=False)
    assert len(our_handlers()) == 1


def test_level_from_argument_and_environment(paths):
    assert configure_logging(paths, "debug", console=False).level == logging.DEBUG
    assert configure_logging(paths, console=False, environ={"ETCODE_LOG_LEVEL": "warning"}).level == logging.WARNING
    assert configure_logging(paths, console=False, environ={"ETCODE_LOG_LEVEL": "nonsense"}).level == logging.INFO
    assert configure_logging(paths, console=False, environ={}).level == logging.INFO


def test_does_not_touch_the_root_logger(paths):
    configure_logging(paths, console=False)
    assert logging.getLogger(PACKAGE_LOGGER).propagate is False


def test_secrets_are_redacted_in_messages_and_tracebacks(paths):
    register_secret("tok-SECRET-123")
    configure_logging(paths, console=False)
    logger = logging.getLogger("etcode_desktop.test")
    logger.warning("sending Authorization: Bearer %s", "tok-SECRET-123")
    try:
        raise RuntimeError("failed with tok-SECRET-123")
    except RuntimeError:
        logger.exception("boom")
    for handler in our_handlers():
        handler.flush()
    text = paths.log_file.read_text()
    assert "tok-SECRET-123" not in text
    assert "Bearer ***" in text and "failed with ***" in text


def test_redact_helpers():
    register_secret("abcdef")
    register_secret("abcdefgh")  # contains the first one: the longer one must win
    register_secret("xy")  # too short: ignored
    register_secret("")
    assert redact("a abcdefgh b abcdef xy") == "a *** b *** xy"
    assert RedactingFormatter("%(message)s").format(
        logging.LogRecord("n", logging.INFO, "f", 1, "x abcdef", None, None)) == "x ***"


def test_falls_back_to_the_console_when_the_log_dir_is_not_writable(paths, tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    from dataclasses import replace
    bad = replace(paths, state_dir=blocker / "state")
    configure_logging(bad, console=False)
    logging.getLogger("etcode_desktop.test").error("still visible")
    assert "still visible" in capsys.readouterr().err


def test_excepthook_logs_and_chains(paths, monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: seen.append(a))
    configure_logging(paths, console=False)
    install_excepthook()
    install_excepthook()  # second call must not wrap again
    try:
        raise ValueError("kaboom")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    for handler in our_handlers():
        handler.flush()
    assert "kaboom" in paths.log_file.read_text()
    assert len(seen) == 1
