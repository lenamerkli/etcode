"""The file tools: ``read_file``, ``write_to_file`` and ``replace_in_file``.

The result texts follow the formats in PLAN.md (section 5.5) byte for byte. Paths in results are echoed as
the model wrote them. Anything that goes wrong is an ``Error: ...`` text (never an exception), because the
model needs an answer for every call.

Files are read and written with ``newline=""``, so line endings are never translated, and offsets
count Python code points, the same way in all three tools. Writes are atomic: the new content goes to a
temporary file next to the target, which then replaces it, so that a reader sees the old or the new file
and never half of one.
"""
from __future__ import annotations

import bisect
import os
import stat
import tempfile
from pathlib import Path

MAX_FILE_BYTES = 200 * 1024 * 1024  # read_file and replace_in_file hold the whole file in memory
BINARY_SNIFF_BYTES = 8192


class FileToolError(Exception):
    """Something the model should hear about. The message is the text after ``Error: ``."""


def resolve_path(path: str, home: Path) -> Path:
    """``~`` is the home directory, and a relative path is relative to it (not to wherever the app runs)."""
    expanded = Path(os.path.expanduser(path))
    return expanded if expanded.is_absolute() else home / expanded


def _read_bytes(path: str, home: Path) -> tuple[Path, bytes]:
    target = resolve_path(path, home)
    try:
        if target.is_dir():
            raise FileToolError(f"{path} is a directory, not a file")
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            raise FileToolError(f"{path} is too large to read here ({size} bytes). Use a shell command such as head, tail or sed")
        data = target.read_bytes()
    except FileNotFoundError:
        raise FileToolError(f"file not found: {path}") from None
    except PermissionError:
        raise FileToolError(f"permission denied: {path}") from None
    except OSError as exc:
        raise FileToolError(f"cannot read {path}: {exc.strerror or exc}") from None
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        raise FileToolError(f"{path} looks like a binary file, not text")
    return target, data


# --- read_file ---------------------------------------------------------------------------------------------

def read_file(path: str, start_line: int, end_line: int, max_chars: int, start_char: int, end_char: int, home: Path) -> str:
    _, data = _read_bytes(path, home)
    content = data.decode("utf-8", errors="replace")  # from bytes, so no newline is translated
    total_chars = len(content)
    lines = content.splitlines(keepends=True)
    line_starts: list[int] = []
    cursor = 0
    for line in lines:
        line_starts.append(cursor)
        cursor += len(line)

    # 1. start at the later of the line and the character
    if 1 <= start_line <= len(lines):
        line_start = line_starts[start_line - 1]
    else:
        line_start = total_chars if start_line > len(lines) else 0
    start = min(max(line_start, start_char, 0), total_chars)

    # 2. stop at the earliest limit: the end of end_line, end_char, or max_chars characters
    if 1 <= end_line < len(lines):
        line_end = line_starts[end_line]
    else:
        line_end = total_chars if end_line >= 1 else 0
    end = max(start, min(line_end, end_char, start + max_chars, total_chars))
    text = content[start:end]

    # 3. report what was read and what was left out
    def line_of(offset: int) -> int:
        return max(bisect.bisect_right(line_starts, offset), 1)

    first_line = line_of(start) if lines else 0
    last_line: int | None = line_of(end - 1) if text else None
    unread_lines = len(lines) - (last_line - first_line + 1) if last_line is not None else len(lines)
    return (
        f"<path>{path}</path>\n<first_char>{start}</first_char>\n<last_char>{end}</last_char>\n"
        f"<first_line>{first_line}</first_line>\n<last_line>{'' if last_line is None else last_line}</last_line>\n"
        f"<total_lines>{len(lines)}</total_lines>\n<unread_chars>{total_chars - len(text)}</unread_chars>\n"
        f"<unread_lines>{unread_lines}</unread_lines>\n<content>{text}</content>"
    )


# --- writing -----------------------------------------------------------------------------------------------

def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def write_atomically(target: Path, text: str) -> None:
    """Replace ``target`` with ``text`` (UTF-8, no newline translation). The mode of an existing file is kept."""
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise FileToolError("the content contains characters that cannot be written as UTF-8 text") from None
    target = Path(os.path.realpath(target))  # write through a symbolic link, instead of replacing the link
    if target.is_dir():
        raise FileToolError(f"{target} is a directory, not a file")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            mode = 0o666 & ~_current_umask()
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
    except PermissionError:
        raise FileToolError(f"permission denied: {target}") from None
    except OSError as exc:
        raise FileToolError(f"cannot write {target}: {exc.strerror or exc}") from None


def write_to_file(path: str, content: str, home: Path) -> str:
    write_atomically(resolve_path(path, home), content)
    return f"Wrote {len(content)} characters to {path}"


# --- replace_in_file ---------------------------------------------------------------------------------------

def replace_in_file(path: str, search: str, replace: str, read: bool, home: Path) -> str:
    if search == "":
        raise FileToolError("search must not be empty")
    target, data = _read_bytes(path, home)
    try:
        text = data.decode("utf-8")  # strictly: a file that is not UTF-8 must not be damaged by a write
    except UnicodeDecodeError:
        raise FileToolError(f"{path} is not valid UTF-8 text, so it cannot be edited with this tool") from None

    occurrences: list[tuple[int, int, int, int]] = []  # old start, old end, new start, new end
    position = 0
    shift = 0  # how much longer the new text is than the old one, before the current occurrence
    while True:
        found = text.find(search, position)
        if found < 0:
            break
        new_start = found + shift
        occurrences.append((found, found + len(search), new_start, new_start + len(replace)))
        shift += len(replace) - len(search)
        position = found + len(search)  # left to right, without overlap

    new_text = text if not occurrences else text.replace(search, replace)
    if occurrences:
        write_atomically(target, new_text)

    output = f"<note>Made {len(occurrences)} replacement(s) in <path>{path}</path></note>\n"
    output += "<occurrences>\n"
    for old_start, old_end, new_start, new_end in occurrences:
        output += "<occurrence>\n"
        output += f"<old_start_character>{old_start}</old_start_character>\n"
        output += f"<old_end_character>{old_end}</old_end_character>\n"
        output += f"<new_start_character>{new_start}</new_start_character>\n"
        output += f"<new_end_character>{new_end}</new_end_character>\n"
        output += "</occurrence>\n"
    output += "</occurrences>\n"
    if read:
        output += f"<content>{new_text}</content>\n"
    return output
