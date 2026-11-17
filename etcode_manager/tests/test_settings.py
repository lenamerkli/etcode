"""Defaults for future VMs: the backend token/URL and the screen scaling algorithm."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etcode_manager import config as manager_config
from etcode_manager.config import (
    DEFAULT_BACKEND_URL, DEFAULT_SCALING, Settings, load_settings, normalize_scaling, save_settings,
)
from etcode_manager.scaling import ScalingFilter
from etcode_manager.vm import VMManager



class ScalingConfigTest(unittest.TestCase):
    def test_normalize_scaling(self):
        for value, expected in (
            ("", DEFAULT_SCALING),
            (" area ", "area"),
            ("AREA", "area"),
            ("lanczos", "lanczos"),
            ("Sharper", "lanczos"),
            (ScalingFilter.LANCZOS, "lanczos"),
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_scaling(value), expected)
        for bad in ("nearest", "smooth-ish", 5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_scaling(bad)

    def test_load_reads_the_scaling_default_and_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            for content, expected in (
                ({"scaling": "lanczos"}, "lanczos"),
                ({"scaling_filter": "Sharper"}, "lanczos"),
                ({}, DEFAULT_SCALING),
            ):
                with self.subTest(content=content):
                    cfg.write_text(json.dumps(content))
                    with patch.object(manager_config, "CONFIG_FILE", cfg):
                        settings = load_settings()
                    self.assertEqual(settings.scaling, expected)
                    self.assertEqual(settings.scaling_filter, ScalingFilter(expected))

    def test_invalid_scaling_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"scaling": "nearest"}))
            with patch.object(manager_config, "CONFIG_FILE", cfg), self.assertRaises(RuntimeError) as caught:
                load_settings()
        self.assertIn("scaling", str(caught.exception))

    def test_save_preserves_other_keys_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({
                "base_image": "/tmp/base.qcow2",
                "custom": {"keep": True},
                "scaling_filter": "area",  # legacy alias is migrated to the canonical key
            }))
            settings = Settings(
                backend_url="https://srv.example/api/", backend_token=" new-token ",
                scaling="lanczos",
            )
            result = save_settings(settings, cfg)
            self.assertEqual(result, cfg)
            data = json.loads(cfg.read_text())
            self.assertEqual(data["base_image"], "/tmp/base.qcow2")
            self.assertEqual(data["custom"], {"keep": True})
            self.assertEqual(data["backend_url"], "https://srv.example/api")
            self.assertEqual(data["backend_token"], "new-token")
            self.assertEqual(data["scaling"], "lanczos")
            self.assertNotIn("scaling_filter", data)
            self.assertEqual(sorted(p.name for p in cfg.parent.glob("config.json*")), ["config.json"])
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)

    def test_saved_defaults_are_loaded_for_future_vms(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            settings = Settings(backend_url="https://srv.example", backend_token="tok", scaling="lanczos")
            with patch.object(manager_config, "CONFIG_FILE", cfg):
                save_settings(settings)
                loaded = load_settings()
            self.assertEqual((loaded.backend_url, loaded.backend_token, loaded.scaling),
                             ("https://srv.example", "tok", "lanczos"))

    def test_clearing_the_token_removes_the_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            with patch.object(manager_config, "CONFIG_FILE", cfg):
                save_settings(Settings(backend_token="secret"))
                self.assertIn("secret", cfg.read_text())
                save_settings(Settings(backend_token=""))
                self.assertNotIn("secret", cfg.read_text())
                self.assertEqual(load_settings().backend_token, "")

    def test_invalid_existing_config_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text("{not json")
            with self.assertRaises(RuntimeError):
                save_settings(Settings(backend_token="tok"), cfg)
            self.assertEqual(cfg.read_text(), "{not json")


class SettingsDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_prefilled_and_masked(self):
        from PySide6.QtWidgets import QLineEdit
        from etcode_manager.app import SettingsDialog

        dialog = SettingsDialog(
            backend_url="https://srv.example", backend_token="tok", scaling=ScalingFilter.LANCZOS
        )
        self.assertEqual(dialog.values(), ("https://srv.example", "tok", ScalingFilter.LANCZOS))
        self.assertEqual(dialog.defaults(), dialog.values())
        self.assertEqual(dialog.scaling_combo.currentData(), ScalingFilter.LANCZOS)
        self.assertEqual(dialog.token_edit.echoMode(), QLineEdit.EchoMode.Password)
        dialog.show_token.setChecked(True)
        self.assertEqual(dialog.token_edit.echoMode(), QLineEdit.EchoMode.Normal)

    def test_values_are_cleaned(self):
        from etcode_manager.app import SettingsDialog
        dialog = SettingsDialog()
        dialog.backend_url_edit.setText("https://srv.example/api/")
        dialog.token_edit.setText(" tok \n")
        dialog.scaling_combo.setCurrentIndex(1)
        self.assertEqual(dialog.values(), ("https://srv.example/api", "tok", ScalingFilter.LANCZOS))

    def test_invalid_url_keeps_the_dialog_open(self):
        from PySide6.QtWidgets import QDialog
        from etcode_manager.app import SettingsDialog
        dialog = SettingsDialog()
        dialog.show()
        dialog.backend_url_edit.setText("not a url")
        dialog.accept()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertFalse(dialog.error_label.isHidden())
        self.assertIn("http", dialog.error_label.text())
        dialog.close()


class MainWindowSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self, tmp, **settings_kwargs):
        from etcode_manager.app import MainWindow
        settings = Settings(vms_dir=Path(tmp) / "vms", **settings_kwargs)
        return MainWindow(settings, VMManager(settings))

    def test_window_starts_with_the_configured_scaling_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = self.make_window(tmp, scaling="lanczos")
            try:
                self.assertEqual(window.scaling_combo.currentData(), ScalingFilter.LANCZOS)
                self.assertEqual(window.screen.scaling_filter, ScalingFilter.LANCZOS)
            finally:
                window.close()

    def test_settings_action_persists_and_updates_the_window(self):
        from PySide6.QtWidgets import QDialog
        from etcode_manager import app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            window = self.make_window(tmp, backend_url=DEFAULT_BACKEND_URL, backend_token="old")
            try:
                class FakeDialog(app_module.SettingsDialog):
                    def __init__(self, parent=None, **kwargs):
                        super().__init__(parent, **kwargs)
                        self.backend_url_edit.setText("https://new.example")
                        self.token_edit.setText("new-token")
                        self.scaling_combo.setCurrentIndex(1)

                    def exec(self):
                        return QDialog.DialogCode.Accepted

                with patch.object(manager_config, "CONFIG_FILE", cfg), \
                        patch.object(app_module, "SettingsDialog", FakeDialog):
                    window.on_settings()

                self.assertEqual(window.settings.backend_url, "https://new.example")
                self.assertEqual(window.settings.backend_token, "new-token")
                self.assertEqual(window.settings.scaling, "lanczos")
                self.assertEqual(window.scaling_combo.currentData(), ScalingFilter.LANCZOS)
                self.assertEqual(window.screen.scaling_filter, ScalingFilter.LANCZOS)
                data = json.loads(cfg.read_text())
                self.assertEqual((data["backend_token"], data["scaling"]), ("new-token", "lanczos"))
            finally:
                window.close()

    def test_new_vm_dialog_gets_the_new_default_token(self):
        from PySide6.QtWidgets import QDialog
        from etcode_manager import app as app_module

        class FakeNewDialog(app_module.NewVMDialog):
            seen = {}

            def __init__(self, parent=None, **kwargs):
                super().__init__(parent, **kwargs)
                FakeNewDialog.seen = kwargs
                self.name_edit.setText("vm-next")

            def exec(self):
                return QDialog.DialogCode.Accepted

        class FakeSettingsDialog(app_module.SettingsDialog):
            def __init__(self, parent=None, **kwargs):
                super().__init__(parent, **kwargs)
                self.token_edit.setText("future-token")

            def exec(self):
                return QDialog.DialogCode.Accepted

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            window = self.make_window(tmp)
            try:
                with patch.object(manager_config, "CONFIG_FILE", cfg), \
                        patch.object(app_module, "SettingsDialog", FakeSettingsDialog):
                    window.on_settings()
                from etcode_manager.vm import VM
                with patch.object(app_module, "NewVMDialog", FakeNewDialog), \
                        patch.object(window.manager, "create", return_value=VM(id="future", name="vm-next")):
                    window.on_new()
                self.assertEqual(FakeNewDialog.seen["backend_token"], "future-token")
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
