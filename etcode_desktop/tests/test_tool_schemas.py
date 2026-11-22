import sys
from pathlib import Path

import pytest

from etcode_desktop.core.tools.schemas import SCHEMAS, TOOL_NAMES, ToolArgumentError, UnknownTool, explicit, validate_arguments
from etcode_desktop.core.tools.schemas_data import CLIENT_TOOLS

BACKEND_DIR = Path(__file__).resolve().parents[2] / "etcode_backend"


def test_the_client_tools_are_known():
    assert TOOL_NAMES == {"bash", "interactive_terminal", "read_file", "write_to_file", "replace_in_file", "save_messages", "view_image"}
    assert "websearch" not in TOOL_NAMES  # the backend runs it


# --- the copy must equal the backend's definitions --------------------------------------------------------------------------

def test_the_copy_equals_the_backends_schemas():
    if BACKEND_DIR.is_dir():
        sys.path.insert(0, str(BACKEND_DIR))
    pytest.importorskip("etcode_backend.tools", reason="the backend is not next to etcode_desktop (or lacks its dependencies)")
    from etcode_backend.tools import BACKEND_TOOLS, tools_for
    advertised = {t["function"]["name"]: t["function"] for t in tools_for(True, True)}
    assert set(advertised) - set(BACKEND_TOOLS) == TOOL_NAMES, "the backend and the client disagree about who runs which tool"
    for tool in CLIENT_TOOLS:
        assert tool == advertised[tool["name"]], f"the schema of {tool['name']} differs from the backend's: update schemas_data.py"


# --- validation -------------------------------------------------------------------------------------------------------------------

def test_defaults_are_filled_in():
    assert validate_arguments("bash", {"command": "ls"}) == {
        "command": "ls", "timeout": 60, "directory": "/home/agent", "max_chars": 100000}
    assert validate_arguments("read_file", {"path": "a"}) == {
        "path": "a", "start_line": 1, "end_line": 1000, "max_chars": 1_000_000, "start_char": 0, "end_char": 100_000}
    assert validate_arguments("replace_in_file", {"path": "a", "search": "x", "replace": "y"}) == {"path": "a", "search": "x", "replace": "y"}
    assert validate_arguments("write_to_file", {"path": "a", "content": ""}) == {"path": "a", "content": ""}
    assert validate_arguments("view_image", {"path": "i.png"}) == {"path": "i.png"}
    assert validate_arguments("save_messages", {"directory": "export"}) == {"directory": "export"}


def test_given_values_win_over_defaults():
    args = validate_arguments("bash", {"command": "ls", "timeout": 5, "directory": "/tmp", "venv": "/v", "max_chars": 10})
    assert args == {"command": "ls", "timeout": 5, "directory": "/tmp", "venv": "/v", "max_chars": 10}


def test_a_null_counts_as_not_given():
    assert validate_arguments("bash", {"command": "ls", "timeout": None})["timeout"] == 60
    with pytest.raises(ToolArgumentError, match="missing required argument 'command'"):
        validate_arguments("bash", {"command": None})


def test_integral_floats_are_integers_but_booleans_are_not():
    assert validate_arguments("bash", {"command": "x", "timeout": 30.0})["timeout"] == 30
    for bad in (30.5, True, "30", [30]):
        with pytest.raises(ToolArgumentError, match="'timeout' must be an integer"):
            validate_arguments("bash", {"command": "x", "timeout": bad})


@pytest.mark.parametrize("tool, arguments, message", [
    ("bash", {}, "missing required argument 'command'"),
    ("bash", {"command": 5}, "'command' must be a string"),
    ("read_file", {}, "missing required argument 'path'"),
    ("write_to_file", {"path": "a"}, "missing required argument 'content'"),
    ("write_to_file", {"path": "a", "content": 5}, "'content' must be a string"),
    ("replace_in_file", {"path": "a", "search": "x"}, "missing required argument 'replace'"),
    ("replace_in_file", {"path": "a", "search": "x", "replace": "y", "read": "yes"}, "'read' must be true or false"),
    ("view_image", {}, "missing required argument 'path'"),
    ("save_messages", {}, "missing required argument 'directory'"),
    ("save_messages", {"directory": 5}, "'directory' must be a string"),
])
def test_bad_arguments_say_what_is_wrong(tool, arguments, message):
    with pytest.raises(ToolArgumentError, match=message):
        validate_arguments(tool, arguments)


def test_extra_arguments_are_ignored():
    assert "surprise" not in validate_arguments("bash", {"command": "ls", "surprise": 1})


def test_unreadable_arguments_from_the_backend():
    with pytest.raises(ToolArgumentError, match="not valid JSON: {oops"):
        validate_arguments("bash", {"_invalid_arguments": "{oops"})


def test_an_unknown_tool_lists_the_known_ones():
    with pytest.raises(UnknownTool) as info:
        validate_arguments("rm_rf", {})
    assert "unknown tool 'rm_rf'" in str(info.value) and "bash" in str(info.value) and "websearch" not in str(info.value)


def test_explicit():
    assert explicit({"a": 1}, "a") and not explicit({"a": None}, "a") and not explicit({}, "a")
    assert set(SCHEMAS) == TOOL_NAMES
