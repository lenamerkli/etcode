"""Qt key event -> X11 keysym translation for the RFB client.

For printable keys the character produced by the key event (``event.text()``)
is the keysym, which gives correct behaviour for Shift/AltGr combinations.
Non-printable keys (arrows, function keys, modifiers, ...) use the table
below.  Control characters (e.g. Ctrl+A producing ``\\x01``) fall back to the
base key so that Ctrl combos work.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent

# X11 keysyms
XK_Escape = 0xFF1B
XK_Tab = 0xFF09
XK_BackSpace = 0xFF08
XK_Return = 0xFF0D
XK_KP_Enter = 0xFF8D
XK_Insert = 0xFF63
XK_Delete = 0xFFFF
XK_Home = 0xFF50
XK_End = 0xFF57
XK_Page_Up = 0xFF55
XK_Page_Down = 0xFF56
XK_Left = 0xFF51
XK_Up = 0xFF52
XK_Right = 0xFF53
XK_Down = 0xFF54
XK_Shift_L = 0xFFE1
XK_Shift_R = 0xFFE2
XK_Control_L = 0xFFE3
XK_Control_R = 0xFFE4
XK_Alt_L = 0xFFE9
XK_Alt_R = 0xFFEA
XK_Super_L = 0xFFEB
XK_Super_R = 0xFFEC
XK_Caps_Lock = 0xFFE5
XK_Num_Lock = 0xFF7F
XK_Scroll_Lock = 0xFF14
XK_Print = 0xFF61
XK_Pause = 0xFF13
XK_Menu = 0xFF67

_F_KEYS_BASE = 0xFFBE  # XK_F1

SPECIAL_KEYSYMS = {
    Qt.Key.Key_Escape: XK_Escape,
    Qt.Key.Key_Tab: XK_Tab,
    Qt.Key.Key_Backtab: XK_Tab,
    Qt.Key.Key_Backspace: XK_BackSpace,
    Qt.Key.Key_Return: XK_Return,
    Qt.Key.Key_Enter: XK_KP_Enter,
    Qt.Key.Key_Insert: XK_Insert,
    Qt.Key.Key_Delete: XK_Delete,
    Qt.Key.Key_Home: XK_Home,
    Qt.Key.Key_End: XK_End,
    Qt.Key.Key_PageUp: XK_Page_Up,
    Qt.Key.Key_PageDown: XK_Page_Down,
    Qt.Key.Key_Left: XK_Left,
    Qt.Key.Key_Up: XK_Up,
    Qt.Key.Key_Right: XK_Right,
    Qt.Key.Key_Down: XK_Down,
    Qt.Key.Key_Shift: XK_Shift_L,
    Qt.Key.Key_Control: XK_Control_L,
    Qt.Key.Key_Alt: XK_Alt_L,
    Qt.Key.Key_Meta: XK_Super_L,
    Qt.Key.Key_Super_L: XK_Super_L,
    Qt.Key.Key_Super_R: XK_Super_R,
    Qt.Key.Key_AltGr: 0xFE03,  # XK_ISO_Level3_Shift
    Qt.Key.Key_CapsLock: XK_Caps_Lock,
    Qt.Key.Key_NumLock: XK_Num_Lock,
    Qt.Key.Key_ScrollLock: XK_Scroll_Lock,
    Qt.Key.Key_Print: XK_Print,
    Qt.Key.Key_Pause: XK_Pause,
    Qt.Key.Key_Menu: XK_Menu,
}

# Qt.Key_A .. Qt.Key_Z are contiguous, likewise Key_0 .. Key_9.
_QT_KEY_A = int(Qt.Key.Key_A)
_QT_KEY_Z = int(Qt.Key.Key_Z)
_QT_KEY_0 = int(Qt.Key.Key_0)
_QT_KEY_9 = int(Qt.Key.Key_9)


def key_event_to_keysym(event: QKeyEvent) -> int | None:
    """Return the X11 keysym for a Qt key event, or None if unmappable."""
    key = event.key()
    text = event.text()

    if text:
        char = text[0]
        code = ord(char)
        if code >= 0x20 and code != 0x7F:
            # Printable character produced (handles Shift / AltGr).
            return code if code < 0x100 else 0x01000000 | code

    if key in SPECIAL_KEYSYMS:
        return SPECIAL_KEYSYMS[key]

    # Control combos yield control characters in text(); use the base key.
    if _QT_KEY_A <= key <= _QT_KEY_Z:
        return 0x61 + (key - _QT_KEY_A)  # 'a' .. 'z'
    if _QT_KEY_0 <= key <= _QT_KEY_9:
        return 0x30 + (key - _QT_KEY_0)  # '0' .. '9'

    return None