import pytest

from etcode_desktop.core.attachments import (
    MAX_IMAGE_BYTES, Attachment, AttachmentError, attachment_from_bytes, load_attachment, sniff_image,
)
from etcode_desktop.core.options import EFFORTS, SessionOptions

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 30
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20
GIF = b"GIF89a" + b"\x00" * 20


# --- options ----------------------------------------------------------------------------------------------------------------

def test_default_options_send_nothing():
    options = SessionOptions()
    assert options.is_default and options.overrides() == {}


def test_only_what_was_chosen_is_sent():
    assert SessionOptions(temperature=0.5).overrides() == {"temperature": 0.5}
    assert SessionOptions(0, 100, "high").overrides() == {"temperature": 0, "max_tokens": 100, "reasoning_effort": "high"}
    assert not SessionOptions(temperature=0).is_default  # zero is a choice


@pytest.mark.parametrize("kwargs", [
    {"temperature": -0.1}, {"temperature": 2.5}, {"temperature": True}, {"temperature": "hot"},
    {"max_tokens": 0}, {"max_tokens": -5}, {"max_tokens": 1.5}, {"max_tokens": True}, {"reasoning_effort": "extreme"},
])
def test_invalid_options_are_refused(kwargs):
    with pytest.raises(ValueError):
        SessionOptions(**kwargs)


def test_the_limits_are_accepted():
    SessionOptions(temperature=0)
    SessionOptions(temperature=2)
    SessionOptions(max_tokens=1)
    for effort in EFFORTS:
        SessionOptions(reasoning_effort=effort)


def test_options_from_what_the_backend_reports_ignore_what_does_not_fit():
    assert SessionOptions.from_mapping({"temperature": 0.3, "max_tokens": 50, "reasoning_effort": "low"}) == SessionOptions(0.3, 50, "low")
    assert SessionOptions.from_mapping({"temperature": None, "max_tokens": None}) == SessionOptions()
    assert SessionOptions.from_mapping({"temperature": 99, "max_tokens": "many", "reasoning_effort": "low"}) == SessionOptions(reasoning_effort="low")
    assert SessionOptions.from_mapping(None) == SessionOptions() and SessionOptions.from_mapping({"unknown": 1}) == SessionOptions()


def test_options_round_trip_through_json():
    options = SessionOptions(0.7, 2000, "medium")
    assert SessionOptions.from_json(options.to_json()) == options
    assert SessionOptions.from_json(SessionOptions().to_json()) == SessionOptions()
    for broken in (None, "", "{not json", "[1, 2]", '"text"'):
        assert SessionOptions.from_json(broken) == SessionOptions()


# --- attachments --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("data, mime", [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp"), (GIF, "image/gif")])
def test_the_four_formats_are_recognised_by_their_content(data, mime):
    assert sniff_image(data) == mime
    attachment = attachment_from_bytes(data, "whatever.bin")
    assert attachment == Attachment("whatever.bin", data, mime) and attachment.size == len(data)


@pytest.mark.parametrize("data", [b"not an image", b"\x89PNG", b"RIFF\x00\x00\x00\x00WAVEdata", b"%PDF-1.7"])
def test_other_content_is_refused(data):
    with pytest.raises(AttachmentError, match="is not a PNG, JPEG, WebP or GIF image"):
        attachment_from_bytes(data, "file.png")  # the name says png, the content says no


def test_empty_and_huge_images_are_refused():
    with pytest.raises(AttachmentError, match="is empty"):
        attachment_from_bytes(b"", "a.png")
    with pytest.raises(AttachmentError, match="too large"):
        attachment_from_bytes(PNG + b"\x00" * MAX_IMAGE_BYTES, "big.png")


def test_a_file_is_loaded_and_checked(tmp_path):
    (tmp_path / "pic.png").write_bytes(PNG)
    assert load_attachment(tmp_path / "pic.png") == Attachment("pic.png", PNG, "image/png")
    assert load_attachment(str(tmp_path / "pic.png")).mime == "image/png"


def test_file_problems_are_explained(tmp_path, monkeypatch):
    with pytest.raises(AttachmentError, match="nope.png does not exist"):
        load_attachment(tmp_path / "nope.png")
    (tmp_path / "folder").mkdir()
    with pytest.raises(AttachmentError, match="is a folder"):
        load_attachment(tmp_path / "folder")
    (tmp_path / "text.png").write_text("hello")
    with pytest.raises(AttachmentError, match="not a PNG"):
        load_attachment(tmp_path / "text.png")
    monkeypatch.setattr("etcode_desktop.core.attachments.MAX_IMAGE_BYTES", 10)
    (tmp_path / "big.png").write_bytes(PNG)
    with pytest.raises(AttachmentError, match="too large"):
        load_attachment(tmp_path / "big.png")  # refused from the size, before it is read
