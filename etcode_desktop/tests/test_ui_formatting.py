import time

import pytest

pytest.importorskip("PySide6")

from etcode_desktop.core.store import SessionRecord  # noqa: E402
from etcode_desktop.ui import formatting as f  # noqa: E402


def record(**kw):
    base = dict(id="abc", backend_url="u", token_fp="t", title="", title_custom=False, model="fake", status="idle",
                cost_total=None, created_at=1000.0, updated_at=1000.0, last_opened=None, last_seen_index=None, archived=False)
    base.update(kw)
    return SessionRecord(**base)


def test_display_title():
    assert f.display_title(record(title="Fix it")) == "Fix it"
    assert f.display_title(record(title="")) == f.UNTITLED == "New session"
    assert f.display_title(record(title="   ")) == f.UNTITLED


@pytest.mark.parametrize("age, expected", [
    (0, "just now"), (59, "just now"), (60, "1 min ago"), (119, "1 min ago"), (3599, "59 min ago"),
    (3600, "1 h ago"), (86399, "23 h ago"), (86400, "yesterday"), (2 * 86400, "2 days ago"), (6 * 86400, "6 days ago"),
])
def test_format_age(age, expected):
    assert f.format_age(10_000_000.0 - age, 10_000_000.0) == expected


def test_format_age_for_old_dates_and_the_future():
    ts = time.mktime((2026, 1, 15, 12, 0, 0, 0, 0, -1))
    assert f.format_age(ts, ts + 30 * 86400) == "2026-01-15"
    assert f.format_age(500.0, 100.0) == "just now"  # a clock that is a little off


def test_format_cost():
    assert f.format_cost("0.0123") == "$0.0123" and f.format_cost("0") == "$0"
    assert f.format_cost(None) == f.format_cost("") == ""


def test_status_text_and_colours_cover_every_status():
    for status in ("idle", "generating", "awaiting_tools", "failed", "missing"):
        assert f.status_color(status).startswith("#") and f.status_label(status)
    assert f.status_label("generating") == "working" and f.status_label("awaiting_tools") == "running tools"
    assert f.status_label("something new") == f.status_label(None) == "unknown"
    assert len({f.status_color(s) for s in ("idle", "generating", "awaiting_tools", "failed")}) == 4


def test_subtitle_and_tooltip():
    now = 1000.0 + 300
    text = f.subtitle(record(model="fake", updated_at=1000.0), now)
    assert text == "fake · 5 min ago"
    assert f.subtitle(record(status="missing", archived=True), now) == "fake · not on the backend · 5 min ago · removed"
    assert f.subtitle(record(model=None), now) == "5 min ago"
    tip = f.tooltip(record(title="T", cost_total="0.5", status="failed"), now)
    for expected in ("T", "Status: failed", "Model: fake", "Cost: $0.5", "Last activity: 5 min ago", "Id: abc"):
        assert expected in tip


# --- the conversation ------------------------------------------------------------------------------------------

def test_truncate():
    assert f.truncate("short", 10) == "short"
    assert f.truncate("x" * 20, 10) == "x" * 9 + "…"
    assert f.truncate("abcdef", 3, "...") == "abc" and f.truncate("abcdef", 2, "...") == "ab"  # no room for the marker
    assert f.truncate("abcdefgh", 6, "...") == "abc..." and len(f.truncate("x" * 50, 10)) == 10
    assert f.truncate("", 5) == ""


@pytest.mark.parametrize("name, arguments, expected", [
    ("bash", {"command": "ls -la /tmp"}, "ls -la /tmp"),
    ("bash", {"command": "echo one\necho two"}, "echo one"),
    ("bash", {"command": "x" * 200}, "x" * 89 + "…"),
    ("read_file", {"path": "/etc/hostname", "start_line": 3}, "/etc/hostname"),
    ("write_to_file", {"path": "a.py", "content": "..."}, "a.py"),
    ("replace_in_file", {"path": "b.py", "search": "x", "replace": "y"}, "b.py"),
    ("view_image", {"path": "shot.png"}, "shot.png"),
    ("interactive_gui", {"window_id": "0x00600007", "action": "hold", "key": "w"}, "hold 0x00600007"),
    ("websearch", {"query": "python docs", "count": 3}, "python docs"),
    ("something_new", {"alpha": 1, "beta": 2}, "alpha=1"),
    ("bash", {}, ""),
    ("bash", {"command": ""}, ""),
    ("bash", {"command": 5}, "command=5"),
    ("bash", {"_invalid_arguments": "{oops"}, "(arguments are not valid JSON)"),
])
def test_tool_summary(name, arguments, expected):
    assert f.tool_summary(name, arguments) == expected


def test_parts_text_and_image_label():
    assert f.parts_text(None) == f.parts_text([]) == ""
    assert f.parts_text([{"type": "text", "text": "a"}, {"type": "image", "hash": "h" * 64}, {"type": "text", "text": "b"}]) == "a\n[image]\nb"
    assert f.image_label("abcdef1234567890" * 4) == "[image abcdef12…]"
