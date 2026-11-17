"""execute_tool: the one entry point. It must always answer, whatever the call looks like."""
import pytest

from etcode_desktop.core.tools import ToolContext, execute_tool
from etcode_desktop.core import tools as tools_module


def run(tmp_path, name, arguments):
    return execute_tool(name, arguments, ToolContext(home=tmp_path))


@pytest.mark.parametrize("name, arguments", [
    ("bash", {}), ("bash", {"command": 5}), ("bash", {"command": "x", "timeout": "never"}), ("read_file", {}), ("write_to_file", {"path": "a"}),
    ("replace_in_file", {"path": "a", "search": "b"}), ("view_image", {}), ("nope", {}), ("", {}), ("websearch", {"query": "q"}),
    ("bash", {"_invalid_arguments": "{oops"}), ("read_file", {"path": ["list"]}), ("read_file", {"path": "x", "start_line": "one"}),
])
def test_a_bad_call_is_an_error_text_and_never_an_exception(tmp_path, name, arguments):
    parts = run(tmp_path, name, arguments)
    assert len(parts) == 1 and parts[0]["type"] == "text" and parts[0]["text"].startswith("Error: ")


def test_websearch_is_not_run_here(tmp_path):
    assert "unknown tool 'websearch'" in run(tmp_path, "websearch", {"query": "q"})[0]["text"]


def test_a_bug_in_a_tool_still_gives_an_answer(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("the tool has a bug")

    monkeypatch.setattr(tools_module, "read_file", broken)
    parts = run(tmp_path, "read_file", {"path": "x"})
    assert parts == [{"type": "text", "text": "Error: the tool read_file failed: RuntimeError: the tool has a bug"}]


def test_results_are_always_non_empty_text_parts(tmp_path):
    (tmp_path / "e.txt").write_text("")
    for name, arguments in (("read_file", {"path": "e.txt"}), ("write_to_file", {"path": "w.txt", "content": ""}),
                            ("bash", {"command": "true", "directory": str(tmp_path)})):
        parts = run(tmp_path, name, arguments)
        assert parts and all(p["type"] == "text" and p["text"] for p in parts)
