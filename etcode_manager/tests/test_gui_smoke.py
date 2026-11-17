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

    def test_vnc_view_scales_smoothly(self):
        """Enlarging a tiny framebuffer must interpolate rather than repeat pixels.

        The separate scaling regressions cover reduction of a 1920x1080
        framebuffer and the difference between bilinear and area filtering.
        """
        from PySide6.QtGui import QImage, QColor
        from etcode_manager.view import VncView

        image = QImage(2, 2, QImage.Format.Format_RGB32)
        image.setPixelColor(0, 0, QColor(0, 0, 0))
        image.setPixelColor(1, 0, QColor(255, 255, 255))
        image.setPixelColor(0, 1, QColor(255, 255, 255))
        image.setPixelColor(1, 1, QColor(0, 0, 0))

        view = VncView()
        view.resize(640, 400)
        try:
            view.show()
            self.app.processEvents()
            view._set_image(image)
            self.app.processEvents()
            rendered = view.grab().toImage()
        finally:
            view.close()

        grey = sum(
            1
            for y in range(0, rendered.height(), 2)
            for x in range(0, rendered.width(), 2)
            if 16 < rendered.pixelColor(x, y).red() < 239
        )
        self.assertGreater(grey, 0, "the screen is scaled with nearest-neighbour filtering")

    def test_vnc_view_forwards_hover_without_click(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QImage
        from PySide6.QtTest import QTest
        from etcode_manager.view import VncView

        class PointerClient:
            def __init__(self):
                self.events = []

            def send_pointer(self, mask, x, y):
                self.events.append((mask, x, y))

        view = VncView()
        view.resize(640, 400)
        view._image = QImage(160, 100, QImage.Format.Format_RGB32)
        view._image.fill(0)
        client = PointerClient()
        view._client = client
        try:
            view.show()
            self.app.processEvents()  # paint the image and establish its scale
            QTest.mouseMove(view, QPoint(200, 120))
            self.app.processEvents()
            self.assertEqual(client.events[-1], (0, 50, 30))
        finally:
            view.close()

    def test_screen_scaling_selector_switches_filters(self):
        import tempfile
        from etcode_manager.config import Settings
        from etcode_manager.vm import VMManager
        from etcode_manager.app import MainWindow
        from etcode_manager.scaling import ScalingFilter

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(vms_dir=Path(tmp) / "vms")
            window = MainWindow(settings, VMManager(settings))
            try:
                self.assertEqual(window.scaling_combo.currentData(), ScalingFilter.AREA)
                self.assertEqual(window.screen.scaling_filter, ScalingFilter.AREA)
                window.scaling_combo.setCurrentIndex(1)
                self.assertEqual(window.screen.scaling_filter, ScalingFilter.LANCZOS)
                window.scaling_combo.setCurrentIndex(0)
                self.assertEqual(window.screen.scaling_filter, ScalingFilter.AREA)
            finally:
                window.close()

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