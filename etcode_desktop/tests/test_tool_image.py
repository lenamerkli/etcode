import pytest

pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from etcode_desktop.core.tools import ToolContext, execute_tool  # noqa: E402
from etcode_desktop.core.tools import image as image_module  # noqa: E402
from fake_backend import PNG  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20
GIF = b"GIF89a" + b"\x00" * 20


def view(tmp_path, client, path="pic.png"):
    return execute_tool("view_image", {"path": path}, ToolContext(client=client, home=tmp_path))


def test_an_image_is_uploaded_and_its_hash_returned(fake, make_client, tmp_path):
    (tmp_path / "pic.png").write_bytes(PNG)
    parts = view(tmp_path, make_client(fake))
    uploaded = next(iter(fake.images))[1]
    assert parts == [{"type": "image", "hash": uploaded}, {"type": "text", "text": f"Image pic.png (image/png, {len(PNG)} bytes)"}]
    assert fake.images[("tok-a", uploaded)][0] == PNG


@pytest.mark.parametrize("data, mime", [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")])
def test_the_three_formats(fake, make_client, tmp_path, data, mime):
    (tmp_path / "x.img").write_bytes(data)
    parts = view(tmp_path, make_client(fake), "x.img")
    assert parts[0]["type"] == "image" and mime in parts[1]["text"]


@pytest.mark.parametrize("data", [GIF, b"not an image at all", b"", b"\x89PNG"])
def test_other_files_are_refused_before_any_upload(fake, make_client, tmp_path, data):
    (tmp_path / "x.img").write_bytes(data)
    parts = view(tmp_path, make_client(fake), "x.img")
    assert parts == [{"type": "text", "text": "Error: x.img is not a PNG, JPEG or WebP image"}]
    assert not fake.requests_to("POST", "/media/images")


def test_the_extension_does_not_matter_only_the_content(fake, make_client, tmp_path):
    (tmp_path / "actually.txt").write_bytes(PNG)
    assert view(tmp_path, make_client(fake), "actually.txt")[0]["type"] == "image"
    (tmp_path / "fake.png").write_text("this is text")
    assert view(tmp_path, make_client(fake), "fake.png")[0]["text"].startswith("Error: fake.png is not a PNG")


def test_errors_for_the_file(fake, make_client, tmp_path, monkeypatch):
    client = make_client(fake)
    assert view(tmp_path, client, "missing.png")[0]["text"] == "Error: file not found: missing.png"
    (tmp_path / "d").mkdir()
    assert view(tmp_path, client, "d")[0]["text"] == "Error: d is a directory, not an image"
    monkeypatch.setattr(image_module, "MAX_IMAGE_BYTES", 10)
    (tmp_path / "big.png").write_bytes(PNG)
    assert view(tmp_path, client, "big.png")[0]["text"].startswith(f"Error: big.png is too large ({len(PNG)} bytes)")
    assert not fake.requests_to("POST", "/media/images")


def test_a_refusal_by_the_backend_is_an_error_text(fake, make_client, tmp_path):
    (tmp_path / "pic.png").write_bytes(PNG)
    fake.fail_next("POST", "/media/images", status=413, code="invalid_image", message="Image is empty or exceeds the upload size limit")
    text = view(tmp_path, make_client(fake))[0]["text"]
    assert text == "Error: the backend did not accept the image: Image is empty or exceeds the upload size limit"


def test_an_unreachable_backend_is_an_error_text(make_client, tmp_path):
    (tmp_path / "pic.png").write_bytes(PNG)
    text = view(tmp_path, make_client("http://127.0.0.1:9"))[0]["text"]
    assert text.startswith("Error: the image could not be uploaded:")


def test_without_a_client_there_is_a_clear_error(tmp_path):
    (tmp_path / "pic.png").write_bytes(PNG)
    assert view(tmp_path, None)[0]["text"] == "Error: images cannot be uploaded: there is no connection to the backend"


def test_a_relative_path_is_relative_to_home(fake, make_client, tmp_path):
    (tmp_path / "shots").mkdir()
    (tmp_path / "shots" / "a.png").write_bytes(PNG)
    assert view(tmp_path, make_client(fake), "shots/a.png")[0]["type"] == "image"
