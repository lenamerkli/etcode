import os
import stat
import threading

import pytest

from etcode_desktop.core.tools import ToolContext, execute_tool
from etcode_desktop.core.tools import files as files_module


def call(tmp_path, tool, **arguments):
    parts = execute_tool(tool, arguments, ToolContext(home=tmp_path))
    assert len(parts) == 1 and parts[0]["type"] == "text"
    return parts[0]["text"]


def read(tmp_path, **arguments):
    return call(tmp_path, "read_file", **arguments)


def fields(text):
    """The metadata of a read_file result, plus the content."""
    values = {}
    for tag in ("path", "first_char", "last_char", "first_line", "last_line", "total_lines", "unread_chars", "unread_lines"):
        values[tag] = text.split(f"<{tag}>")[1].split(f"</{tag}>")[0]
    values["content"] = text.split("<content>", 1)[1][:-len("</content>")]
    return values


# --- read_file: the exact format and the agreed examples ---------------------------------------------------------------------------

def test_the_exact_format(tmp_path):
    (tmp_path / "f.txt").write_text("a\nbb\nccc\n")
    assert read(tmp_path, path="f.txt") == (
        "<path>f.txt</path>\n<first_char>0</first_char>\n<last_char>9</last_char>\n<first_line>1</first_line>\n"
        "<last_line>3</last_line>\n<total_lines>3</total_lines>\n<unread_chars>0</unread_chars>\n"
        "<unread_lines>0</unread_lines>\n<content>a\nbb\nccc\n</content>")


@pytest.mark.parametrize("arguments, first_char, last_char, first_line, last_line, unread_chars, unread_lines, content", [
    ({}, 0, 9, 1, "3", 0, 0, "a\nbb\nccc\n"),
    ({"end_line": 2}, 0, 5, 1, "2", 4, 1, "a\nbb\n"),
    ({"start_line": 2, "end_line": 2}, 2, 5, 2, "2", 6, 2, "bb\n"),
    ({"start_line": 3, "start_char": 0}, 5, 9, 3, "3", 5, 2, "ccc\n"),  # the later start wins
    ({"start_char": 4, "end_char": 6}, 4, 6, 2, "3", 7, 1, "\ncc"[:2]),  # ends inside line 3
    ({"end_char": 3}, 0, 3, 1, "2", 6, 1, "a\nb"),  # ends inside line 2
    ({"max_chars": 4}, 0, 4, 1, "2", 5, 1, "a\nbb"),
    ({"start_char": 100}, 9, 9, 3, "", 9, 3, ""),
])
def test_the_agreed_examples(tmp_path, arguments, first_char, last_char, first_line, last_line, unread_chars, unread_lines, content):
    (tmp_path / "f.txt").write_text("a\nbb\nccc\n")
    values = fields(read(tmp_path, path="f.txt", **arguments))
    assert values == {"path": "f.txt", "first_char": str(first_char), "last_char": str(last_char), "first_line": str(first_line),
                      "last_line": last_line, "total_lines": "3", "unread_chars": str(unread_chars),
                      "unread_lines": str(unread_lines), "content": content}


def test_the_empty_last_line_element_is_present_but_empty(tmp_path):
    (tmp_path / "f.txt").write_text("a\n")
    assert "<last_line></last_line>" in read(tmp_path, path="f.txt", start_char=50)


def test_the_path_is_echoed_as_the_model_wrote_it(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("x")
    assert fields(read(tmp_path, path="./sub/../sub/f.txt"))["path"] == "./sub/../sub/f.txt"
    assert fields(read(tmp_path, path=str(tmp_path / "sub" / "f.txt")))["path"] == str(tmp_path / "sub" / "f.txt")


def test_relative_paths_are_relative_to_home_and_tilde_is_home(tmp_path, monkeypatch):
    (tmp_path / "f.txt").write_text("home file")
    assert fields(read(tmp_path, path="f.txt"))["content"] == "home file"
    monkeypatch.setenv("HOME", str(tmp_path))
    assert fields(read(tmp_path, path="~/f.txt"))["content"] == "home file"


def test_an_empty_file(tmp_path):
    (tmp_path / "e.txt").write_text("")
    assert fields(read(tmp_path, path="e.txt")) == {"path": "e.txt", "first_char": "0", "last_char": "0", "first_line": "0",
                                                    "last_line": "", "total_lines": "0", "unread_chars": "0", "unread_lines": "0", "content": ""}


def test_a_file_without_a_final_newline(tmp_path):
    (tmp_path / "f.txt").write_text("one\ntwo")
    values = fields(read(tmp_path, path="f.txt"))
    assert (values["total_lines"], values["last_line"], values["last_char"], values["content"]) == ("2", "2", "7", "one\ntwo")


def test_a_single_long_line(tmp_path):
    (tmp_path / "f.txt").write_text("x" * 500)
    values = fields(read(tmp_path, path="f.txt", end_char=100))
    assert (values["first_line"], values["last_line"], values["total_lines"], values["unread_chars"], values["unread_lines"]) == ("1", "1", "1", "400", "0")


def test_crlf_is_not_translated_and_offsets_match_the_file(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"a\r\nbb\r\nccc\r\n")
    values = fields(read(tmp_path, path="f.txt"))
    assert values["content"] == "a\r\nbb\r\nccc\r\n" and values["last_char"] == "12" and values["total_lines"] == "3"
    assert fields(read(tmp_path, path="f.txt", start_line=2, end_line=2))["content"] == "bb\r\n"


def test_offsets_count_characters_not_bytes(tmp_path):
    (tmp_path / "f.txt").write_text("héllo €\nwörld 😀\n", encoding="utf-8")
    values = fields(read(tmp_path, path="f.txt", start_char=8, end_char=12))
    assert values["content"] == "wörl" and values["first_char"] == "8" and values["last_char"] == "12" and values["first_line"] == "2"
    assert values["unread_chars"] == str(len("héllo €\nwörld 😀\n") - 4)


def test_other_line_separators_count_as_line_ends(tmp_path):
    (tmp_path / "f.txt").write_text("a\u2028b\x0cc\n", encoding="utf-8")
    assert fields(read(tmp_path, path="f.txt"))["total_lines"] == "3"


def test_the_start_is_the_later_of_line_and_character_in_both_orders(tmp_path):
    (tmp_path / "f.txt").write_text("a\nbb\nccc\n")
    assert fields(read(tmp_path, path="f.txt", start_line=2, start_char=7))["first_char"] == "7"  # the character is later
    assert fields(read(tmp_path, path="f.txt", start_line=3, start_char=1))["first_char"] == "5"  # the line is later


def test_the_stop_is_the_earliest_of_the_limits(tmp_path):
    (tmp_path / "f.txt").write_text("a\nbb\nccc\n")
    assert fields(read(tmp_path, path="f.txt", end_line=3, end_char=3, max_chars=100))["last_char"] == "3"   # end_char
    assert fields(read(tmp_path, path="f.txt", end_line=1, end_char=8, max_chars=100))["last_char"] == "2"   # end_line
    assert fields(read(tmp_path, path="f.txt", end_line=3, end_char=8, max_chars=1))["last_char"] == "1"     # max_chars


def test_a_line_number_past_the_end_reads_to_the_end(tmp_path):
    (tmp_path / "f.txt").write_text("a\nbb\n")
    assert fields(read(tmp_path, path="f.txt", end_line=500))["content"] == "a\nbb\n"
    assert fields(read(tmp_path, path="f.txt", start_line=500))["content"] == ""
    assert fields(read(tmp_path, path="f.txt", end_line=0))["content"] == ""


def test_defaults_stop_at_line_1000_and_character_100000(tmp_path):
    (tmp_path / "many.txt").write_text("".join(f"line {i}\n" for i in range(1, 3001)))
    values = fields(read(tmp_path, path="many.txt"))
    assert values["last_line"] == "1000" and values["unread_lines"] == "2000" and values["content"].endswith("line 1000\n")
    (tmp_path / "wide.txt").write_text("x" * 250_000 + "\n")
    values = fields(read(tmp_path, path="wide.txt"))
    assert values["last_char"] == "100000" and values["unread_chars"] == "150001"
    # paging on: start where the last one ended, and raise the end
    more = fields(read(tmp_path, path="wide.txt", start_char=100000, end_char=250001))
    assert more["first_char"] == "100000" and more["unread_chars"] == "100000" and len(more["content"]) == 150001


def test_invalid_utf8_is_shown_with_replacement_characters(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"ok \xff\xfe bad")
    assert fields(read(tmp_path, path="f.txt"))["content"] == "ok \ufffd\ufffd bad"


@pytest.mark.parametrize("setup, message", [
    (lambda p: None, "Error: file not found: f.txt"),
    (lambda p: (p / "f.txt").mkdir(), "Error: f.txt is a directory, not a file"),
    (lambda p: (p / "f.txt").write_bytes(b"ab\x00cd"), "Error: f.txt looks like a binary file, not text"),
])
def test_read_errors(tmp_path, setup, message):
    setup(tmp_path)
    assert read(tmp_path, path="f.txt") == message


def test_a_file_that_is_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(files_module, "MAX_FILE_BYTES", 10)
    (tmp_path / "f.txt").write_text("x" * 50)
    assert read(tmp_path, path="f.txt").startswith("Error: f.txt is too large to read here (50 bytes)")


def test_permission_denied(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read everything")
    path = tmp_path / "secret.txt"
    path.write_text("x")
    path.chmod(0)
    assert read(tmp_path, path="secret.txt") == "Error: permission denied: secret.txt"


# --- write_to_file --------------------------------------------------------------------------------------------------------------------

def test_write_reports_characters_and_creates_the_file(tmp_path):
    assert call(tmp_path, "write_to_file", path="new.txt", content="héllo 😀\n") == "Wrote 8 characters to new.txt"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "héllo 😀\n"


def test_write_creates_missing_directories(tmp_path):
    call(tmp_path, "write_to_file", path="a/b/c/file.txt", content="deep")
    assert (tmp_path / "a" / "b" / "c" / "file.txt").read_text() == "deep"


def test_write_overwrites_completely(tmp_path):
    (tmp_path / "f.txt").write_text("old content that is long")
    call(tmp_path, "write_to_file", path="f.txt", content="new")
    assert (tmp_path / "f.txt").read_text() == "new"


def test_write_does_not_translate_newlines(tmp_path):
    call(tmp_path, "write_to_file", path="f.txt", content="a\r\nb\nc\r")
    assert (tmp_path / "f.txt").read_bytes() == b"a\r\nb\nc\r"


def test_write_keeps_the_mode_of_an_existing_file(tmp_path):
    path = tmp_path / "run.sh"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o750)
    call(tmp_path, "write_to_file", path="run.sh", content="#!/bin/sh\necho changed\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o750


def test_a_new_file_gets_the_normal_mode(tmp_path):
    call(tmp_path, "write_to_file", path="new.txt", content="x")
    mask = os.umask(0)
    os.umask(mask)
    assert stat.S_IMODE((tmp_path / "new.txt").stat().st_mode) == 0o666 & ~mask


def test_write_leaves_no_temporary_files(tmp_path):
    call(tmp_path, "write_to_file", path="f.txt", content="x")
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]


def test_write_goes_through_a_symbolic_link(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("old")
    (tmp_path / "link.txt").symlink_to(real)
    call(tmp_path, "write_to_file", path="link.txt", content="new")
    assert (tmp_path / "link.txt").is_symlink() and real.read_text() == "new"


def test_write_to_a_directory_is_an_error(tmp_path):
    (tmp_path / "d").mkdir()
    assert call(tmp_path, "write_to_file", path="d", content="x").startswith("Error: ")
    assert (tmp_path / "d").is_dir()


def test_write_refuses_content_that_is_not_text(tmp_path):
    assert call(tmp_path, "write_to_file", path="f.txt", content="bad \ud800 surrogate") == (
        "Error: the content contains characters that cannot be written as UTF-8 text")
    assert not (tmp_path / "f.txt").exists()


def test_write_where_the_parent_is_a_file(tmp_path):
    (tmp_path / "file").write_text("x")
    assert call(tmp_path, "write_to_file", path="file/inner.txt", content="x").startswith("Error: ")


def test_a_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "f.txt"
    path.write_text("precious")

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(files_module.os, "replace", boom)
    assert call(tmp_path, "write_to_file", path="f.txt", content="new").startswith("Error: cannot write")
    assert path.read_text() == "precious" and [p.name for p in tmp_path.iterdir()] == ["f.txt"]


def test_a_reader_never_sees_half_a_file(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("A" * 200_000)
    seen, stop = set(), threading.Event()

    def reader():
        while not stop.is_set():
            seen.add(len(path.read_text()))

    thread = threading.Thread(target=reader)
    thread.start()
    for i in range(30):
        call(tmp_path, "write_to_file", path="f.txt", content=("B" if i % 2 else "A") * 200_000)
    stop.set()
    thread.join()
    assert seen <= {200_000}


# --- replace_in_file ----------------------------------------------------------------------------------------------------------------------

def occurrence(old_start, old_end, new_start, new_end):
    return (f"<occurrence>\n<old_start_character>{old_start}</old_start_character>\n<old_end_character>{old_end}</old_end_character>\n"
            f"<new_start_character>{new_start}</new_start_character>\n<new_end_character>{new_end}</new_end_character>\n</occurrence>\n")


def replace(tmp_path, content, search, replace_with, **extra):
    (tmp_path / "f.txt").write_text(content, newline="")
    text = call(tmp_path, "replace_in_file", path="f.txt", search=search, replace=replace_with, **extra)
    return text, (tmp_path / "f.txt").read_bytes().decode()


def test_one_replacement_exact_text(tmp_path):
    text, new = replace(tmp_path, "hello world", "world", "there")
    assert text == ("<note>Made 1 replacement(s) in <path>f.txt</path></note>\n<occurrences>\n" + occurrence(6, 11, 6, 11) + "</occurrences>\n")
    assert new == "hello there"


def test_every_occurrence_is_replaced_with_its_offsets(tmp_path):
    text, new = replace(tmp_path, "a-a-a", "a", "XYZ")  # the text grows by 2 each time
    assert new == "XYZ-XYZ-XYZ"
    assert text == ("<note>Made 3 replacement(s) in <path>f.txt</path></note>\n<occurrences>\n"
                    + occurrence(0, 1, 0, 3) + occurrence(2, 3, 4, 7) + occurrence(4, 5, 8, 11) + "</occurrences>\n")


def test_shrinking_replacements_shift_the_new_offsets_back(tmp_path):
    text, new = replace(tmp_path, "xxx-xxx-xxx", "xxx", "y")
    assert new == "y-y-y"
    assert occurrence(0, 3, 0, 1) in text and occurrence(4, 7, 2, 3) in text and occurrence(8, 11, 4, 5) in text


def test_deleting_with_an_empty_replacement(tmp_path):
    text, new = replace(tmp_path, "abcabc", "b", "")
    assert new == "acac" and occurrence(1, 2, 1, 1) in text and occurrence(4, 5, 3, 3) in text


def test_matches_do_not_overlap(tmp_path):
    text, new = replace(tmp_path, "aaaa", "aa", "b")
    assert new == "bb" and text.count("<occurrence>") == 2
    text, new = replace(tmp_path, "aaa", "aa", "b")
    assert new == "ba" and text.count("<occurrence>") == 1


def test_zero_matches_print_the_empty_block_and_leave_the_file_alone(tmp_path):
    (tmp_path / "f.txt").write_text("nothing here")
    before = (tmp_path / "f.txt").stat()
    text = call(tmp_path, "replace_in_file", path="f.txt", search="absent", replace="x")
    assert text == "<note>Made 0 replacement(s) in <path>f.txt</path></note>\n<occurrences>\n</occurrences>\n"
    after = (tmp_path / "f.txt").stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_read_adds_the_whole_file(tmp_path):
    text, new = replace(tmp_path, "one\ntwo\n", "two", "2", read=True)
    assert text.endswith("</occurrences>\n<content>one\n2\n</content>\n")
    text, _ = replace(tmp_path, "one\ntwo\n", "absent", "2", read=True)
    assert text.endswith("</occurrences>\n<content>one\ntwo\n</content>\n")  # unchanged, but shown on request
    text, _ = replace(tmp_path, "one", "one", "1", read=False)
    assert "<content>" not in text


def test_offsets_count_characters(tmp_path):
    text, new = replace(tmp_path, "😀 é €", "é", "e")
    assert occurrence(2, 3, 2, 3) in text and new == "😀 e €"


def test_multi_line_search_and_crlf_files(tmp_path):
    text, new = replace(tmp_path, "a\r\nb\r\nc\r\n", "b\r\nc", "X")
    assert new == "a\r\nX\r\n" and occurrence(3, 7, 3, 4) in text
    text, new = replace(tmp_path, "a\r\nb\r\n", "a", "z")
    assert new == "z\r\nb\r\n"  # the line endings are untouched


def test_the_search_is_exact_not_a_pattern(tmp_path):
    text, new = replace(tmp_path, "a.c abc a*c", "a.c", "X")
    assert new == "X abc a*c"
    text, new = replace(tmp_path, "price (5) [x]", "(5)", "(6)")
    assert new == "price (6) [x]"
    text, new = replace(tmp_path, "Hello hello", "hello", "bye")
    assert new == "Hello bye"  # case matters


def test_replace_keeps_the_mode(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("abc")
    path.chmod(0o750)
    call(tmp_path, "replace_in_file", path="f.txt", search="b", replace="B")
    assert stat.S_IMODE(path.stat().st_mode) == 0o750 and path.read_text() == "aBc"


def test_replace_errors(tmp_path):
    (tmp_path / "f.txt").write_text("abc")
    assert call(tmp_path, "replace_in_file", path="f.txt", search="", replace="x") == "Error: search must not be empty"
    assert call(tmp_path, "replace_in_file", path="missing.txt", search="a", replace="b") == "Error: file not found: missing.txt"
    (tmp_path / "d").mkdir()
    assert call(tmp_path, "replace_in_file", path="d", search="a", replace="b") == "Error: d is a directory, not a file"
    (tmp_path / "bin").write_bytes(b"a\x00b")
    assert "binary" in call(tmp_path, "replace_in_file", path="bin", search="a", replace="b")
    assert (tmp_path / "f.txt").read_text() == "abc"


def test_a_file_that_is_not_utf8_is_not_damaged(tmp_path):
    (tmp_path / "latin.txt").write_bytes("caf\xe9 au lait".encode("latin-1"))
    text = call(tmp_path, "replace_in_file", path="latin.txt", search="au", replace="with")
    assert text == "Error: latin.txt is not valid UTF-8 text, so it cannot be edited with this tool"
    assert (tmp_path / "latin.txt").read_bytes() == "caf\xe9 au lait".encode("latin-1")


def test_write_then_read_then_replace_round_trip(tmp_path):
    call(tmp_path, "write_to_file", path="src/app.py", content="def f():\n    return 1\n")
    assert fields(read(tmp_path, path="src/app.py"))["total_lines"] == "2"
    call(tmp_path, "replace_in_file", path="src/app.py", search="return 1", replace="return 2")
    assert fields(read(tmp_path, path="src/app.py", start_line=2, end_line=2))["content"] == "    return 2\n"
