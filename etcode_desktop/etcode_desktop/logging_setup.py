"""Logging to ``desktop.log`` (rotated, mode 0600) and to stderr.

Secrets are removed from every formatted record, including tracebacks. Register the
token with :func:`register_secret` as soon as it is known.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from collections.abc import Mapping
from types import TracebackType

from .paths import Paths

PACKAGE_LOGGER = "etcode_desktop"
ENV_LOG_LEVEL = "ETCODE_LOG_LEVEL"
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"
MAX_LOG_BYTES = 1_000_000
LOG_BACKUPS = 3
MIN_SECRET_LENGTH = 4  # shorter strings would be redacted all over the place

_secrets: set[str] = set()
_secrets_lock = threading.Lock()
_HANDLER_MARK = "_etcode_desktop_handler"


def register_secret(secret: str) -> None:
    if secret and len(secret) >= MIN_SECRET_LENGTH:
        with _secrets_lock:
            _secrets.add(secret)


def clear_secrets() -> None:
    with _secrets_lock:
        _secrets.clear()


def redact(text: str) -> str:
    with _secrets_lock:
        secrets = sorted(_secrets, key=len, reverse=True)  # longest first, in case one contains another
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler whose files are readable by the owner only."""

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


def _parse_level(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    if value:
        level = logging.getLevelName(value.strip().upper())
        if isinstance(level, int):
            return level
    return logging.INFO


def configure_logging(
    paths: Paths,
    level: str | int | None = None,
    *,
    console: bool = True,
    environ: Mapping[str, str] | None = None,
) -> logging.Logger:
    """Set up the package logger. Safe to call again: the old handlers are replaced."""
    environ = os.environ if environ is None else environ
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(_parse_level(level if level is not None else environ.get(ENV_LOG_LEVEL)))
    logger.propagate = False
    for handler in [h for h in logger.handlers if getattr(h, _HANDLER_MARK, False)]:
        logger.removeHandler(handler)
        handler.close()

    formatter = RedactingFormatter(LOG_FORMAT)
    handlers: list[logging.Handler] = []
    file_error: OSError | None = None
    try:
        paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        handlers.append(_PrivateRotatingFileHandler(paths.log_file, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"))
    except OSError as exc:
        file_error = exc
    if console or not handlers:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_MARK, True)
        logger.addHandler(handler)
    if file_error is not None:
        logger.warning("cannot write the log file %s: %s", paths.log_file, file_error)
    return logger


def install_excepthook() -> None:
    """Send uncaught exceptions to the log. The app runs without a terminal, so stderr is lost."""
    previous = sys.excepthook
    if getattr(previous, _HANDLER_MARK, False):
        return  # already installed

    def hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        logging.getLogger(PACKAGE_LOGGER).critical("uncaught exception", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    setattr(hook, _HANDLER_MARK, True)
    sys.excepthook = hook
