"""Native Qt scan codes and key event pairing in the VNC screen."""

import os
import unittest
from unittest.mock import Mock, call, patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget

from etcode_manager.rfb import RFBClient
from etcode_manager.scancodes import native_scan_to_qnum
from etcode_manager.view import VncView

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class ScanCodeTest(unittest.TestCase):
    def test_xkb_scancodes_are_evdev_plus_eight(self):
        for platform in ("wayland", "wayland-egl", "xcb"):
            for scan, qnum in ((29, 0x15), (94, 0x56), (108, 0xb8), (114, 0xcd),
                               (104, 0x9c), (105, 0x9d), (107, 0x54), (127, 0xc6)):
                with self.subTest(platform=platform, scan=scan):
                    self.assertEqual(native_scan_to_qnum(platform, scan), qnum)

    def test_windows_set_one_scancodes(self):
        for scan, qnum in ((0x56, 0x56), (0xE038, 0xb8), (0xE04D, 0xcd), (0xE01C, 0x9c),
                           (0xE045, 0x45), (0xE037, 0x54), (0x37, 0x37), (0x45, 0xc6)):
            with self.subTest(scan=scan):
                self.assertEqual(native_scan_to_qnum("windows", scan), qnum)

    def test_unmapped_codes_are_not_assumed_to_be_physical_keys(self):
        for platform, scan in (("wayland", 0), ("wayland", 8), ("xcb", 1000),
                               ("offscreen", 94), ("windows", 0), ("windows", 0xE100)):
            self.assertIsNone(native_scan_to_qnum(platform, scan))


class VncKeyboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = VncView()
        self.addCleanup(self.view.close)
        self.client = Mock(spec=RFBClient, ready=True, supports_extended_keys=True)
        self.view._client = self.client
        platform = patch("etcode_manager.view.QGuiApplication.platformName", return_value="wayland")
        platform.start()
        self.addCleanup(platform.stop)

    def send(self, key, scan, *, sym=0, text="", mods=Qt.KeyboardModifier.NoModifier,
             down=True, repeat=False):
        event = QKeyEvent(QEvent.Type.KeyPress if down else QEvent.Type.KeyRelease,
                          key, mods, scan, sym, 0, text, repeat)
        QApplication.sendEvent(self.view, event)
        return event.isAccepted()

    def test_swiss_altgr_and_release_with_different_text(self):
        self.send(Qt.Key.Key_AltGr, 108, sym=0xfe03)
        self.send(0, 26, sym=0x20ac, text="€", mods=Qt.KeyboardModifier.GroupSwitchModifier)
        self.send(Qt.Key.Key_AltGr, 108, sym=0xfe03, down=False)
        self.send(Qt.Key.Key_E, 26, sym=ord("e"), text="e", down=False)
        self.assertEqual(self.client.send_extended_key.call_args_list, [
            call(0xfe03, 0xb8, True), call(0x20ac, 0x12, True),
            call(0xfe03, 0xb8, False), call(0x20ac, 0x12, False),
        ])
        self.client.send_key.assert_not_called()

    def test_dead_keys_without_text_and_space_are_both_forwarded(self):
        self.assertFalse(self.view.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled))
        self.send(Qt.Key.Key_Dead_Circumflex, 21, sym=0xfe52)
        self.send(Qt.Key.Key_Dead_Circumflex, 21, sym=0xfe52, down=False)
        self.send(Qt.Key.Key_Space, 65, sym=0x20, text=" ")
        self.send(Qt.Key.Key_Space, 65, sym=0x20, down=False)
        self.assertEqual(self.client.send_extended_key.call_args_list, [
            call(0xfe52, 0x0d, True), call(0xfe52, 0x0d, False),
            call(0x20, 0x39, True), call(0x20, 0x39, False),
        ])

    def test_unknown_keysym_does_not_prevent_physical_input(self):
        self.assertTrue(self.send(Qt.Key.Key_unknown, 94))
        self.assertTrue(self.send(Qt.Key.Key_unknown, 94, down=False))
        self.assertEqual(self.client.send_extended_key.call_args_list, [call(0, 0x56, True), call(0, 0x56, False)])

    def test_windows_keypad_keeps_its_numlock_keysym(self):
        with patch("etcode_manager.view.QGuiApplication.platformName", return_value="windows"):
            self.send(Qt.Key.Key_1, 0x4f, text="1", mods=Qt.KeyboardModifier.KeypadModifier)
            self.send(Qt.Key.Key_End, 0x4f, down=False)
        self.assertEqual(self.client.send_extended_key.call_args_list, [call(0xffb1, 0x4f, True),
                                                                       call(0xffb1, 0x4f, False)])

    def test_fallback_is_paired_even_if_extension_is_acknowledged_mid_press(self):
        self.client.supports_extended_keys = False
        self.send(Qt.Key.Key_Exclam, 10, text="!")
        self.client.supports_extended_keys = True
        self.send(Qt.Key.Key_1, 10, text="1", down=False)
        self.send(Qt.Key.Key_A, 0, text="a")  # synthetic event, no native code
        self.send(Qt.Key.Key_A, 0, down=False)
        self.assertEqual(self.client.send_key.call_args_list, [
            call(ord("!"), True), call(ord("!"), False), call(ord("a"), True), call(ord("a"), False),
        ])
        self.client.send_extended_key.assert_not_called()

    def test_repeats_do_not_release_key_or_start_a_new_press_after_focus_loss(self):
        self.send(Qt.Key.Key_Z, 29, sym=ord("z"), text="z")
        self.send(Qt.Key.Key_Z, 29, down=False, repeat=True)
        self.send(Qt.Key.Key_Z, 29, sym=ord("z"), text="z", repeat=True)
        QApplication.sendEvent(self.view, QFocusEvent(QEvent.Type.FocusOut))
        self.send(Qt.Key.Key_Z, 29, sym=ord("z"), text="z", repeat=True)
        self.send(Qt.Key.Key_Z, 29, down=False)
        self.assertEqual(self.client.send_extended_key.call_args_list, [
            call(ord("z"), 0x15, True), call(ord("z"), 0x15, True), call(ord("z"), 0x15, False),
        ])

    def test_disconnect_releases_held_keys_in_reverse_order(self):
        self.send(Qt.Key.Key_Shift, 62, sym=0xffe2)
        self.send(Qt.Key.Key_Less, 94, sym=ord(">"), text=">")
        self.view.disconnect_from()
        self.assertEqual(self.client.mock_calls, [
            call.send_extended_key(0xffe2, 0x36, True), call.send_extended_key(ord(">"), 0x56, True),
            call.send_extended_key(ord(">"), 0x56, False), call.send_extended_key(0xffe2, 0x36, False),
            call.close(),
        ])

    def test_tab_is_forwarded_instead_of_moving_manager_focus(self):
        parent = QWidget()
        self.parent = parent  # keep ownership until the view cleanup has run
        self.addCleanup(parent.close)
        layout = QVBoxLayout(parent)
        layout.addWidget(self.view)
        layout.addWidget(QLineEdit(parent))
        parent.show()
        self.view.setFocus()
        self.app.processEvents()
        self.assertTrue(self.view.hasFocus())
        self.send(Qt.Key.Key_Tab, 23, sym=0xff09, text="\t")
        self.send(Qt.Key.Key_Tab, 23, sym=0xff09, down=False)
        self.assertTrue(self.view.hasFocus())
        self.assertEqual(self.client.send_extended_key.call_args_list, [call(0xff09, 0x0f, True),
                                                                       call(0xff09, 0x0f, False)])
