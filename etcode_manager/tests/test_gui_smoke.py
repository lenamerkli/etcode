"""GUI smoke test (runs headless via offscreen platform)."""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class StubRFBClient:
    """Stands in for RFBClient so tests can observe connect/disconnect."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class GuiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_window_and_dialog_construct(self):
        import tempfile
        from etcode_manager.config import Settings
        from etcode_manager.vm import VMManager
        from etcode_manager.app import MainWindow, NewVMDialog

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(vms_dir=Path(tmp) / "vms")
            window = MainWindow(settings, VMManager(settings))
            window.show()
            self.app.processEvents()
            self.assertEqual(window.windowTitle(), "etcode-manager")

            dialog = NewVMDialog()
            dialog.name_edit.setText("task-1")
            self.assertEqual(dialog.values()[0], "task-1")
            window.close()

    def test_vnc_view_paints_without_connection(self):
        from etcode_manager.view import VncView
        view = VncView()
        view.resize(320, 200)
        view.show()
        self.app.processEvents()
        self.assertFalse(view.is_connected)

    # -- screen connection vs. periodic list refresh ------------------------

    def _make_window_with_vms(self, tmp):
        import json
        from etcode_manager.config import Settings
        from etcode_manager.vm import VMManager
        from etcode_manager.app import MainWindow

        vms = Path(tmp) / "vms"
        for vid, name, created in (("aaa", "vm-a", 1.0), ("bbb", "vm-b", 2.0)):
            d = vms / vid
            d.mkdir(parents=True)
            (d / "vm.json").write_text(json.dumps({
                "id": vid, "name": name, "cpus": 2, "memory_mb": 2048,
                "disk_gb": 20, "created": created, "vnc_port": 5900,
                "qmp_port": 0, "pid": 0, "status": "stopped",
            }))
        settings = Settings(vms_dir=vms)
        return MainWindow(settings, VMManager(settings))

    def test_refresh_keeps_screen_connected(self):
        """The periodic refresh must not tear down the screen of the selected VM."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            window = self._make_window_with_vms(tmp)
            try:
                stub = StubRFBClient()
                window.screen._client = stub
                self.assertTrue(window.screen.is_connected)

                window.refresh()  # what the 3 s QTimer does
                window.refresh()

                self.assertTrue(window.screen.is_connected,
                                "refresh() disconnected the screen of the selected VM")
                self.assertFalse(stub.closed)
            finally:
                window.close()

    def test_switching_vm_disconnects_screen(self):
        """Genuinely switching to another VM must still disconnect the screen."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            window = self._make_window_with_vms(tmp)
            try:
                stub = StubRFBClient()
                window.screen._client = stub
                self.assertEqual(window._current.name, "vm-a")

                window.vm_list.setCurrentRow(1)  # user selects the other VM

                self.assertFalse(window.screen.is_connected)
                self.assertTrue(stub.closed)
                self.assertEqual(window._current.name, "vm-b")
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()