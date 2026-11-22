import errno
import hashlib
import json
from pathlib import Path

import pytest

from etcode_desktop.core.errors import AuthError, NotFound
from etcode_desktop.core.models import DownloadedImage, HistoryEntry, image_part, text_part
from etcode_desktop.core.tools import CancelToken, ToolContext, execute_tool, save_messages

PNG = b"\x89PNG\r\n\x1a\nexample image bytes"
PNG_HASH = hashlib.sha256(PNG).hexdigest()


class HistoryClient:
    def __init__(self, history=(), images=None):
        self.history = list(history)
        self.images = images or {}
        self.history_requests = []
        self.image_requests = []

    def messages(self, session_id):
        self.history_requests.append(session_id)
        return self.history

    def download_image(self, image_hash):
        self.image_requests.append(image_hash)
        if image_hash not in self.images:
            raise NotFound(404, "image_not_found", "private error body")
        return self.images[image_hash]


def run(home, client=None, directory="chat-export", *, chat_id="current-chat", cancel=None):
    context = ToolContext(client=client, home=home, chat_id=chat_id, cancel=cancel or CancelToken())
    [part] = execute_tool("save_messages", {"directory": directory}, context)
    assert part["type"] == "text"
    return part["text"]


def entry(index, role, *parts, **fields):
    return HistoryEntry(index, {"role": role, "content": list(parts), **fields})


def assistant(index, text="", *, calls=()):
    return HistoryEntry(index, {"role": "assistant", "text": text_part(text), "tool_calls": list(calls),
                                "reasoning": text_part("not for export"), "status": "complete", "error": None})


def test_exports_the_exact_layout_and_preserves_text_and_image_bytes(tmp_path):
    call = {"id": "r1", "name": "read_file", "arguments": {"path": "café.txt"}}
    user_text = "  héllo 😀\r\nnext line\n"
    client = HistoryClient([
        entry(0, "system", text_part("system prompt")),
        entry(1, "user", text_part(user_text), image_part(PNG_HASH)),
        assistant(2, "I'll read it.", calls=[call]),
        entry(3, "tool", text_part("yes\n"), tool_call_id="r1"),
        assistant(4, "Done!"),
    ], {PNG_HASH: DownloadedImage(PNG, "image/png")})
    destination = tmp_path / "chat-export"

    assert run(tmp_path, client) == f"Saved 5 messages (7 files) to {destination}"
    assert client.history_requests == ["current-chat"]
    assert client.image_requests == [PNG_HASH]
    files = {p.name: p.read_bytes() for p in destination.iterdir()}
    assert json.loads(files.pop("0002_assistant_calls.json")) == [call]
    assert files == {
        "0000-0_system.txt": b"system prompt",
        "0001-0_user.txt": user_text.encode("utf-8"),
        "0001-1_user.png": PNG,
        "0002_assistant.txt": b"I'll read it.",
        "0003-0_tool.txt": b"yes\n",
        "0004_assistant.txt": b"Done!",
    }
    assert not destination.stat().st_mode & 0o077
    assert all(not p.stat().st_mode & 0o077 for p in destination.iterdir())
    assert list(tmp_path.iterdir()) == [destination]


def test_mixed_parts_tool_call_order_and_large_indices(tmp_path):
    calls = [
        {"id": "z", "name": "view_image", "arguments": {"path": "screenshot.jpg"}},
        {"id": "a", "name": "bash", "arguments": {"command": "true", "timeout": 1}},
    ]
    images = {
        "1" * 64: DownloadedImage(b"\xff\xd8\xffsample", "image/jpeg; example=1"),
        "2" * 64: DownloadedImage(b"RIFFdataWEBPsample", "image/webp"),
        "3" * 64: DownloadedImage(b"GIF89asample", "image/gif"),
    }
    jpeg, webp, gif = images
    client = HistoryClient([
        assistant(9999, calls=calls),
        entry(10000, "tool", image_part(jpeg), text_part("image result"), tool_call_id="z"),
        entry(10002, "user", image_part(webp), image_part(gif), text_part("")),
    ], images)
    destination = tmp_path / "missing-parent" / "saved-chat"
    assert run(tmp_path, client, str(destination)) == f"Saved 3 messages (7 files) to {destination}"
    assert client.image_requests == [jpeg, webp, gif]
    files = {p.name: p.read_bytes() for p in destination.iterdir()}
    assert json.loads(files.pop("9999_assistant_calls.json")) == calls
    assert files == {
        "9999_assistant.txt": b"",
        "10000-0_tool.jpg": images[jpeg].data,
        "10000-1_tool.txt": b"image result",
        "10002-0_user.webp": images[webp].data,
        "10002-1_user.gif": images[gif].data,
        "10002-2_user.txt": b"",
    }


@pytest.mark.parametrize("directory", ["", " \t ", "bad\0path"])
def test_invalid_directory_is_rejected_before_fetching_history(tmp_path, directory):
    client = HistoryClient()
    assert run(tmp_path, client, directory).startswith("Error: invalid call of save_messages:")
    assert client.history_requests == [] and not list(tmp_path.iterdir())


def test_requires_a_chat_and_backend_client(tmp_path):
    client = HistoryClient()
    expected = "Error: saving messages requires an active chat and a connection to the backend"
    assert run(tmp_path) == expected
    assert run(tmp_path, client, chat_id="direct") == expected
    assert client.history_requests == [] and not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["directory", "nonempty directory", "file", "dangling symlink"])
def test_existing_destination_is_left_alone(tmp_path, kind):
    destination = tmp_path / "chat-export"
    if kind.endswith("directory"):
        destination.mkdir()
        if kind == "nonempty directory":
            (destination / "untouched").write_bytes(b"existing data")
    elif kind == "file":
        destination.write_bytes(b"existing data")
    else:
        destination.symlink_to(tmp_path / "missing", target_is_directory=True)
    before = destination.lstat()
    client = HistoryClient([entry(0, "system", text_part("new data"))])

    assert "destination already exists" in run(tmp_path, client)
    assert destination.lstat().st_ino == before.st_ino
    if kind == "file":
        assert destination.read_bytes() == b"existing data"
    if kind == "nonempty directory":
        assert (destination / "untouched").read_bytes() == b"existing data"
    assert client.history_requests == [] and list(tmp_path.iterdir()) == [destination]


def test_destination_created_during_export_is_not_replaced(tmp_path, monkeypatch):
    destination = tmp_path / "chat-export"
    client = HistoryClient([entry(1, "user", image_part(PNG_HASH))])
    reserved = []

    def download(_):
        destination.mkdir()
        reserved.append(destination.stat().st_ino)
        return DownloadedImage(PNG, "image/png")

    monkeypatch.setattr(client, "download_image", download)
    assert "destination already exists" in run(tmp_path, client)
    assert destination.stat().st_ino == reserved[0] and not list(destination.iterdir())
    assert list(tmp_path.iterdir()) == [destination]


def test_history_fetch_failure_does_not_echo_backend_error_body(tmp_path, monkeypatch):
    client = HistoryClient()

    def fail(_):
        raise AuthError(401, "unauthorized", "private error body")

    monkeypatch.setattr(client, "messages", fail)
    assert run(tmp_path, client) == "Error: cannot fetch chat history: the backend returned HTTP 401"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("images, expected", [
    ({}, "cannot download image for 0001-1_user: the backend returned HTTP 404"),
    ({PNG_HASH: DownloadedImage(b"raw", "image/x-other")}, "unsupported or empty image in 0001-1_user"),
])
def test_unavailable_or_unsupported_images_leave_no_partial_export(tmp_path, images, expected):
    client = HistoryClient([entry(1, "user", text_part("already staged"), image_part(PNG_HASH))], images)
    assert run(tmp_path, client) == f"Error: {expected}"
    assert not list(tmp_path.iterdir())


def test_unsupported_content_type_leaves_no_partial_export(tmp_path):
    client = HistoryClient([entry(0, "system", text_part("already staged"), {"type": "other"})])
    assert run(tmp_path, client) == "Error: unsupported content type in 0000-1_system"
    assert not list(tmp_path.iterdir())


def test_cancel_after_downloading_leaves_no_partial_export(tmp_path, monkeypatch):
    cancel = CancelToken()
    client = HistoryClient([entry(1, "user", text_part("already staged"), image_part(PNG_HASH))])

    def download(_):
        cancel.cancel()
        return DownloadedImage(PNG, "image/png")

    monkeypatch.setattr(client, "download_image", download)
    assert run(tmp_path, client, cancel=cancel) == "Error: saving messages was cancelled"
    assert not list(tmp_path.iterdir())


def test_write_failure_removes_staging_files(tmp_path, monkeypatch):
    client = HistoryClient([entry(0, "system", text_part("already staged"), text_part("no room"))])
    original = save_messages._write_private

    def fail_on_second_file(path, data, cancel):
        if path.name == "0000-1_system.txt":
            raise OSError(errno.ENOSPC, "No space left on device")
        original(path, data, cancel)

    monkeypatch.setattr(save_messages, "_write_private", fail_on_second_file)
    assert run(tmp_path, client).endswith(": No space left on device")
    assert not list(tmp_path.iterdir())


def test_publish_failure_removes_the_empty_reservation(tmp_path, monkeypatch):
    destination = tmp_path / "chat-export"
    client = HistoryClient([entry(0, "system", text_part("fully staged"))])

    def fail(_, target):
        assert target == destination and not list(destination.iterdir())
        raise OSError(errno.EIO, "rename failed")

    monkeypatch.setattr(Path, "rename", fail)
    assert run(tmp_path, client).endswith(": rename failed")
    assert not list(tmp_path.iterdir())
