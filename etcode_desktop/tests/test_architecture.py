"""The non-UI layers must stay importable (and therefore testable) without Qt."""
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "etcode_desktop"
NON_UI_MODULES = ["etcode_desktop", "etcode_desktop.paths", "etcode_desktop.config",
                  "etcode_desktop.logging_setup", "etcode_desktop.core", "etcode_desktop.core.errors",
                  "etcode_desktop.core.models", "etcode_desktop.core.api", "etcode_desktop.core.sse",
                  "etcode_desktop.core.store", "etcode_desktop.core.sync", "etcode_desktop.core.transcript",
                  "etcode_desktop.core.controller", "etcode_desktop.core.session_state", "etcode_desktop.core.describe",
                  "etcode_desktop.core.tools", "etcode_desktop.core.tools.bash", "etcode_desktop.core.tools.files",
                  "etcode_desktop.core.tools.image", "etcode_desktop.core.tools.scheduler", "etcode_desktop.core.tools.schemas",
                  "etcode_desktop.core.tools.cancel", "etcode_desktop.core.options", "etcode_desktop.core.attachments"]


def test_non_ui_modules_import_without_qt():
    code = "import sys; sys.modules['PySide6'] = None\n" + "\n".join(f"import {m}" for m in NON_UI_MODULES)
    result = subprocess.run([sys.executable, "-c", code], cwd=PACKAGE.parent, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_core_package_never_mentions_qt():
    for path in (PACKAGE / "core").rglob("*.py"):
        text = path.read_text()
        assert "PySide6" not in text and "PyQt" not in text, path


def test_version_flag_works_without_qt():
    code = "import sys; sys.modules['PySide6'] = None\nfrom etcode_desktop.__main__ import main\nmain(['--version'])"
    result = subprocess.run([sys.executable, "-c", code], cwd=PACKAGE.parent, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.startswith("etcode-desktop ")
