"""Convert Qt native scan codes to QEMU's RFB keycode (qnum) format."""

# Linux evdev -> qnum from qemu/keycodemapdb commit 22b8996dba9041874845c7446ce89ec4ae2b713d.
# Generated with `keymap-gen code-map --lang=python3 keymaps.csv linux qnum`,
# replacing None with 0 and dropping trailing zeroes. See ../licenses/keycodemapdb.BSD.
_EVDEV_TO_QNUM = bytes.fromhex(
    "00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f"
    "10 11 12 13 14 15 16 17 18 19 1a 1b 1c 1d 1e 1f"
    "20 21 22 23 24 25 26 27 28 29 2a 2b 2c 2d 2e 2f"
    "30 31 32 33 34 35 36 37 38 39 3a 3b 3c 3d 3e 3f"
    "40 41 42 43 44 45 46 47 48 49 4a 4b 4c 4d 4e 4f"
    "50 51 52 53 54 76 56 57 58 73 78 77 79 70 7b 5c"
    "9c 9d b5 54 b8 5b c7 c8 c9 cb cd cf d0 d1 d2 d3"
    "ef a0 ae b0 de 59 ce c6 8b 7e 72 71 7d db dc dd"
    "e8 85 86 87 8c f8 64 65 c1 bc f5 9e a1 66 df e3"
    "67 68 69 93 9f 97 82 6a 92 6b a6 ec e6 eb ea e9"
    "a3 6c fd 99 a2 90 a4 b1 98 63 00 81 b2 e7 00 00"
    "88 75 8f f6 fb 89 8a 5d 5e 5f 55 83 f7 84 5a 74"
    "f9 6d 6f 95 96 9a 9b a7 a8 a9 ab ac ad a5 af b3"
    "b4 b6 b9 ba bb bd be bf c0 e5 c2 c3 c4 c5 94 ca"
    "cc d4 ed d6 d7 d8 d9 da e4 8e d5 f0 f1 f2 f3 f4"
)


def native_scan_to_qnum(platform: str, scan: int) -> int | None:
    """Map a Qt QKeyEvent.nativeScanCode(), or return None for an unknown key."""
    if platform == "xcb" or platform.startswith("wayland"):
        # Both Qt xcb and Qt Wayland expose XKB keycodes (Linux evdev + 8).
        # Wayland's wl_keyboard sends evdev, but Qt adds the offset before delivery.
        evdev = scan - 8
        if 0 <= evdev < len(_EVDEV_TO_QNUM):
            return _EVDEV_TO_QNUM[evdev] or None
    elif platform == "windows":
        # Qt's Windows plugin uses set 1 with the E0 prefix in the high byte.
        # Windows reports Pause and PrintScreen differently from the RFB convention.
        if scan in (0x45, 0xE046):  # Pause / Ctrl+Break; Num Lock has the E0 flag
            return 0xC6
        if scan == 0xE045:  # Num Lock
            return 0x45
        if scan == 0xE037:  # PrintScreen / SysRq; 0x37 alone is keypad multiply
            return 0x54
        if 0 < scan < 0x7F:
            return scan
        if 0xE001 <= scan < 0xE07F:
            return (scan & 0x7F) | 0x80
    return None
