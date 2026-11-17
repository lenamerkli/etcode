"""Entry point: ``python -m etcode_desktop``."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Sequence

from . import __version__
from .config import ConfigStore, KEY_TOKEN, KEY_URL
from .core.store import SessionStore, StoreError
from .core.tools.scheduler import ToolRunner
from .logging_setup import configure_logging, install_excepthook, register_secret
from .paths import Paths

log = logging.getLogger("etcode_desktop.main")  # not __name__: under `python -m` that is "__main__", outside the package logger


def parse_args(argv: Sequence[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    """Return our options and the leftovers, which are passed on to Qt (for example ``-platform offscreen``)."""
    parser = argparse.ArgumentParser(prog="etcode-desktop", description="GUI for the etcode backend, for use inside an etcode VM.")
    parser.add_argument("--version", action="version", version=f"etcode-desktop {__version__}")
    parser.add_argument("--maximized", action="store_true", help="open the window maximized (used by autostart)")
    parser.add_argument("--no-single-instance", action="store_true", help="skip the one-window-per-user check (development)")
    return parser.parse_known_args(argv)


def install_signal_handlers(app) -> None:
    """Quit properly on SIGTERM, SIGINT and SIGHUP (the VM shutting down, say), so that the commands of the model are
    ended and nothing is left behind. Python only runs signal handlers when it gets control, so a timer gives it some."""
    from PySide6.QtCore import QTimer

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: app.quit())
    timer = QTimer(app)
    timer.setInterval(300)
    timer.timeout.connect(lambda: None)
    timer.start()  # owned by the app, so it runs as long as the app does


def main(argv: Sequence[str] | None = None) -> int:
    args, qt_args = parse_args(argv)
    paths = Paths.discover()
    paths.ensure_dirs()
    configure_logging(paths)
    install_excepthook()

    settings = ConfigStore(paths).load()
    register_secret(settings.token)
    log.info(
        "starting etcode-desktop %s (backend %s from %s, token %s)",
        __version__, settings.backend_url, settings.source_of(KEY_URL),
        f"from {settings.source_of(KEY_TOKEN)}" if settings.has_token else "not configured",
    )

    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from .ui.bridge import BackendBridge
        from .ui.image_loader import ImageLoader
        from .ui.main_window import MainWindow
        from .ui.single_instance import SingleInstance
    except ImportError as exc:
        log.critical("cannot load Qt: %s", exc)
        print(f"error: cannot load PySide6 ({exc}). Is it installed with its system libraries?", file=sys.stderr)
        return 1

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("etcode-desktop")
    app.setApplicationDisplayName("etcode")

    guard = None
    if not args.no_single_instance:
        guard = SingleInstance(paths.lock_file)
        if not guard.acquire():
            log.info("another instance is running, asked it to show its window")
            return 0

    try:
        store = SessionStore(paths.store_file)
    except (StoreError, OSError) as exc:
        log.critical("cannot open the session store %s: %s", paths.store_file, exc)
        print(f"error: cannot open the session store {paths.store_file}: {exc}", file=sys.stderr)
        if guard is not None:
            guard.release()
        return 1

    try:
        pruned = store.journal_prune()
        if pruned:
            log.info("forgot %d old tool calls from the journal", pruned)
    except Exception:
        log.exception("could not prune the tool journal")
    # Tool calls of one round now run one at a time; the pool only bounds concurrent sessions.
    # ``max_parallel_tools`` is deprecated and ignored.
    tools = ToolRunner(store=store)
    bridge = BackendBridge(store, settings)
    config = ConfigStore(paths)
    pictures = ImageLoader(paths.data_dir / "images", lambda: bridge.client)
    bridge.reconnected.connect(pictures.forget_failures)
    window = MainWindow(settings, store, bridge, tools=tools, config=config, image_loader=pictures)
    app.aboutToQuit.connect(window.shutdown)
    install_signal_handlers(app)
    if guard is not None:
        guard.raise_requested.connect(window.bring_to_front)
    if args.maximized:
        window.showMaximized()
    else:
        window.show()
    bridge.start()
    if not settings.has_token:
        QTimer.singleShot(0, window.prompt_for_missing_token)  # a VM made without a token: nothing works until there is one
    try:
        return app.exec()
    finally:
        bridge.stop()
        tools.shutdown()
        tools.pool.shutdown()
        store.close()
        if guard is not None:
            guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
