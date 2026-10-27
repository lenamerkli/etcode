"""Command-line entry point: ``python -m etcode_manager``."""

from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from .config import load_settings
    from .vm import VMManager
    from .app import MainWindow

    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName("etcode-manager")

    window = MainWindow(settings, VMManager(settings))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
