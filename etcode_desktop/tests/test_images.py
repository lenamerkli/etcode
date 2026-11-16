"""Pictures: attaching, uploading, the cache, thumbnails (offscreen Qt, fake backend)."""
import os
import stat
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from PySide6.QtCore import QMimeData, QSize, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QMessageBox, QPushButton, QToolButton  # noqa: E402

from etcode_desktop.core import controller as c  # noqa: E402
from etcode_desktop.core.attachments import MAX_ATTACHMENTS, Attachment  # noqa: E402
from etcode_desktop.core.tools.scheduler import ToolPool, ToolRunner  # noqa: E402
from etcode_desktop.ui import composer as composer_module  # noqa: E402
from etcode_desktop.ui import image_loader as loader_module  # noqa: E402
from etcode_desktop.ui.composer import Composer, png_bytes  # noqa: E402
from etcode_desktop.ui.image_loader import ImageLoader, ImageViewerDialog, ThumbnailLabel, pixmap_from_bytes  # noqa: E402
from fake_backend import Reply  # noqa: E402
from test_chat_pane import blocks, settled, texts  # noqa: E402
from test_controller import Harness  # noqa: E402
from test_gui_sessions import Env, wait_until  # noqa: E402


def make_png(width=400, height=300, color="#3366cc"):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return png_bytes(image)


def pump(qapp, condition, timeout=5.0):
    return wait_until(qapp, condition, timeout)


@pytest.fixture
def loader(qapp, fake, make_client, tmp_path):
    client = make_client(fake)
    created = ImageLoader(tmp_path / "cache", lambda: client)
    created.client = client
    return created


def uploaded(loader, data=None):
    return loader.client.upload_image(data or make_png(), "p.png").hash


def media_gets(fake, image_hash=None):
    return [r for r in fake.requests if r.method == "GET" and r.path.startswith("/media/images/") and (image_hash is None or r.path.endswith(image_hash))]


def ask(qapp, loader, image_hash, kind="thumbnail"):
    results = []
    getattr(loader, kind)(image_hash, lambda pixmap, error: results.append((pixmap, error)))
    return results


# --- the loader --------------------------------------------------------------------------------------------------------------------

def test_a_picture_is_downloaded_scaled_to_a_thumbnail_and_cached(qapp, fake, loader):
    h = uploaded(loader, make_png(400, 300))
    results = ask(qapp, loader, h)
    assert pump(qapp, lambda: results)
    pixmap, error = results[0]
    assert error is None and pixmap.width() <= 240 and pixmap.height() <= 180 and (pixmap.width() == 240 or pixmap.height() == 180)
    assert len(media_gets(fake, h)) == 1 and loader.path_of(h).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_second_request_is_answered_at_once_from_memory(qapp, fake, loader):
    h = uploaded(loader)
    first = ask(qapp, loader, h)
    assert pump(qapp, lambda: first)
    second = ask(qapp, loader, h)
    assert len(second) == 1 and second[0][1] is None  # already there: no waiting, no thread
    assert len(media_gets(fake, h)) == 1


def test_a_small_picture_is_not_scaled_up(qapp, fake, loader):
    h = uploaded(loader, make_png(50, 40))
    results = ask(qapp, loader, h)
    assert pump(qapp, lambda: results) and (results[0][0].width(), results[0][0].height()) == (50, 40)


def test_the_disk_cache_serves_a_new_loader_without_the_backend(qapp, fake, loader, tmp_path, make_client):
    h = uploaded(loader)
    assert pump(qapp, lambda: ask(qapp, loader, h) or True)
    time.sleep(0.2)
    qapp.processEvents()
    fake.requests.clear()
    fresh = ImageLoader(tmp_path / "cache", lambda: None)  # no client at all: it must not need one
    results = ask(qapp, fresh, h)
    assert pump(qapp, lambda: results) and results[0][1] is None and media_gets(fake) == []


def test_requests_for_the_same_picture_share_one_download(qapp, fake, loader):
    h = uploaded(loader)
    a, b, c_ = ask(qapp, loader, h), ask(qapp, loader, h), ask(qapp, loader, h)
    assert pump(qapp, lambda: a and b and c_)
    assert len(media_gets(fake, h)) == 1


def test_the_full_picture_is_not_scaled_down_by_the_loader(qapp, fake, loader):
    h = uploaded(loader, make_png(600, 500))
    results = ask(qapp, loader, h, "full")
    assert pump(qapp, lambda: results) and (results[0][0].width(), results[0][0].height()) == (600, 500)


def test_a_picture_that_the_app_uploaded_is_never_downloaded(qapp, fake, loader):
    data = make_png()
    h = uploaded(loader, data)
    loader.prime(h, data)
    results = ask(qapp, loader, h)
    assert pump(qapp, lambda: results) and results[0][1] is None and media_gets(fake) == []


def test_a_missing_picture_is_reported_and_not_asked_for_again_for_a_while(qapp, fake, loader):
    missing = "ab" * 32
    results = ask(qapp, loader, missing)
    assert pump(qapp, lambda: results)
    assert results[0][0] is None and "Image not found" in results[0][1]
    again = ask(qapp, loader, missing)
    assert len(again) == 1 and again[0][0] is None  # answered at once, from the memory of the failure
    assert len(media_gets(fake)) == 1
    loader.forget_failures()
    retry = ask(qapp, loader, missing)
    assert pump(qapp, lambda: retry) and len(media_gets(fake)) == 2


def test_failures_are_forgotten_after_a_while(qapp, fake, loader, monkeypatch):
    missing = "cd" * 32
    assert pump(qapp, lambda: ask(qapp, loader, missing) or True)
    time.sleep(0.2)
    qapp.processEvents()
    monkeypatch.setattr(loader_module, "RETRY_AFTER_FAILURE_SECONDS", 0.0)
    retry = ask(qapp, loader, missing)
    assert pump(qapp, lambda: retry) and len(media_gets(fake)) == 2


def test_without_a_client_and_with_a_bad_id(qapp, tmp_path):
    offline = ImageLoader(tmp_path / "cache", lambda: None)
    results = ask(qapp, offline, "ef" * 32)
    assert pump(qapp, lambda: results) and "no connection" in results[0][1]
    bad = ask(qapp, offline, "../../etc/passwd")
    assert bad == [(None, "not a picture id")]
    assert ask(qapp, offline, "A" * 64) == [(None, "not a picture id")]


def test_bytes_that_are_not_a_picture_are_reported(qapp, fake, loader):
    h = loader.client.upload_image(make_png(), "p.png").hash
    loader.path_of(h).write_bytes(b"\x89PNG\r\n\x1a\n" + b"not really a picture")  # a damaged cache file
    results = ask(qapp, loader, h)
    assert pump(qapp, lambda: results) and results[0][0] is None and "cannot be shown" in results[0][1]


def test_a_callback_for_a_deleted_widget_does_not_break_the_others(qapp, fake, loader):
    h = uploaded(loader)
    seen = []

    def deleted_widget(pixmap, error):
        raise RuntimeError("Internal C++ object already deleted")

    loader.thumbnail(h, deleted_widget)
    loader.thumbnail(h, lambda pixmap, error: seen.append(error))
    assert pump(qapp, lambda: seen) and seen == [None]


def test_the_cache_is_private(qapp, tmp_path):
    ImageLoader(tmp_path / "private" / "cache", lambda: None)
    assert stat.S_IMODE((tmp_path / "private" / "cache").stat().st_mode) == 0o700


def test_cached_files_are_private_and_complete(qapp, fake, loader):
    h = uploaded(loader)
    assert pump(qapp, lambda: ask(qapp, loader, h) or True)
    time.sleep(0.2)
    path = loader.path_of(h)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == [h]  # no half-written files left


def test_the_cache_is_pruned_oldest_first_and_only_pictures_are_touched(qapp, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    names = [f"{i:02x}" * 32 for i in range(5)]
    for age, name in enumerate(names):  # names[0] is the oldest
        path = cache / name
        path.write_bytes(b"x" * 1000)
        os.utime(path, (1000 + age, 1000 + age))
    (cache / "notes.txt").write_text("not a picture")
    loader = ImageLoader(cache, lambda: None, max_cache_bytes=3500)  # 5000 bytes are in it: over the limit
    left = sorted(p.name for p in cache.iterdir())
    assert "notes.txt" in left
    assert names[0] not in left and names[1] not in left and names[4] in left  # the oldest went
    assert sum(p.stat().st_size for p in cache.iterdir() if p.name != "notes.txt") <= 3500 * 0.8 + 1000
    assert loader.prune() == 0  # under the limit now


def test_using_a_picture_makes_it_the_last_to_be_pruned(qapp, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    old, newer = "aa" * 32, "bb" * 32
    for age, name in enumerate((old, newer)):
        (cache / name).write_bytes(b"x" * 1000)
        os.utime(cache / name, (1000 + age, 1000 + age))
    loader = ImageLoader(cache, lambda: None, max_cache_bytes=10_000)
    assert loader._bytes(old) == b"x" * 1000  # used just now
    loader._max_bytes = 1500
    loader.prune()
    assert (cache / old).exists() and not (cache / newer).exists()


def test_only_the_last_thumbnails_are_kept_in_memory(qapp, fake, loader, monkeypatch):
    monkeypatch.setattr(loader_module, "MEMORY_THUMBNAILS", 2)
    hashes = [uploaded(loader, make_png(60 + i, 50)) for i in range(4)]
    for h in hashes:
        results = ask(qapp, loader, h)
        assert pump(qapp, lambda: results)
    assert list(loader._thumbnails) == hashes[-2:]


# --- thumbnails and the viewer --------------------------------------------------------------------------------------------------------

def test_a_thumbnail_label_shows_the_placeholder_and_then_the_picture(qapp, fake, loader):
    h = uploaded(loader)
    label = ThumbnailLabel(loader)
    label.show_hash(h)
    assert label.text().startswith("[image ") and not label.has_picture()
    assert pump(qapp, label.has_picture) and label.text() == "" and label.width() <= 240


def test_a_thumbnail_label_with_bytes_shows_them_at_once_without_a_download(qapp, fake, loader):
    label = ThumbnailLabel(loader)
    label.show_hash("12" * 32, make_png(100, 80), "mine.png")
    assert label.has_picture() and media_gets(fake) == []
    other = ThumbnailLabel(loader)
    other.show_bytes(make_png(100, 80), "not-sent.png")
    assert other.has_picture() and "not-sent.png" in other.toolTip()


def test_a_picture_that_cannot_be_loaded_says_so(qapp, loader):
    label = ThumbnailLabel(loader)
    label.show_hash("fe" * 32)
    assert pump(qapp, lambda: "not available" in label.text())
    assert "Image not found" in label.text() and not label.has_picture()


def test_a_click_shows_the_picture_large(qapp, fake, loader, monkeypatch):
    opened = []
    monkeypatch.setattr(loader_module, "open_viewer", lambda parent, ld, image_hash, data, title: opened.append((image_hash, data, title)))
    label = ThumbnailLabel(loader)
    label.show_hash("12" * 32, make_png(100, 80), "mine.png")
    label.show()
    QTest.mouseClick(label, Qt.MouseButton.LeftButton)
    assert opened == [("12" * 32, opened[0][1], "mine.png")] and opened[0][1][:4] == b"\x89PNG"
    QTest.mouseClick(label, Qt.MouseButton.RightButton)
    assert len(opened) == 1  # only the left button


def test_the_viewer_shows_a_picture_from_bytes_or_from_the_backend(qapp, fake, loader):
    dialog = ImageViewerDialog(None, None, make_png(300, 200), "from bytes")
    assert dialog.shown_pixmap() is not None and dialog.shown_pixmap().width() == 300
    h = uploaded(loader, make_png(320, 240))
    remote = ImageViewerDialog(loader, h, None, "from the backend")
    assert pump(qapp, lambda: remote.shown_pixmap() is not None) and remote.shown_pixmap().width() == 320


def test_the_viewer_scales_a_picture_that_is_bigger_than_the_screen(qapp):
    dialog = ImageViewerDialog(None, None, make_png(5000, 4000), "huge")
    pixmap = dialog.shown_pixmap()
    assert pixmap is not None and pixmap.width() <= dialog._limit.width() and pixmap.height() <= dialog._limit.height()
    assert abs(pixmap.width() / pixmap.height() - 5000 / 4000) < 0.01


def test_the_viewer_says_when_there_is_nothing_to_show(qapp):
    assert ImageViewerDialog(None, None, None, "nothing").shown_pixmap() is None
    dialog = ImageViewerDialog(None, None, b"not a picture", "broken")
    assert dialog.shown_pixmap() is None and "cannot be shown" in dialog._label.text()


def test_pixmap_from_bytes_limits_the_size(qapp):
    pixmap = pixmap_from_bytes(make_png(1000, 800))
    assert pixmap.width() <= 240 and pixmap.height() <= 180
    assert pixmap_from_bytes(make_png(1000, 800), None).width() == 1000
    assert pixmap_from_bytes(b"junk") is None
    assert pixmap_from_bytes(make_png(100, 100), QSize(10, 10)).width() == 10


# --- the composer --------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def composer(qapp):
    box = Composer()
    box.show()
    box.sent = []
    box.send_requested.connect(box.sent.append)
    yield box
    box.close()


def png_file(tmp_path, name="pic.png", **kw):
    path = tmp_path / name
    path.write_bytes(make_png(**kw))
    return path


def test_attaching_a_file_shows_a_chip_and_it_goes_with_the_message(qapp, composer, tmp_path, monkeypatch):
    path = png_file(tmp_path, "holiday.png")
    monkeypatch.setattr(composer_module, "choose_image_files", lambda parent: [str(path)])
    composer.attach_button.click()
    assert [a.name for a in composer.attachments()] == ["holiday.png"] and len(composer.chips) == 1
    assert not composer._strip.isHidden()
    composer.set_text("what is this?")
    composer.send_button.click()
    assert composer.sent == ["what is this?"] and composer.attachments()[0].mime == "image/png"


def test_a_picture_alone_can_be_sent(qapp, composer, tmp_path):
    composer.send_button.click()
    assert composer.sent == []  # nothing to send
    composer.add_attachment(Attachment("a.png", make_png(), "image/png"))
    composer.send_button.click()
    assert composer.sent == [""]


def test_a_chip_can_be_removed(qapp, composer):
    one, two = Attachment("one.png", make_png(), "image/png"), Attachment("two.png", make_png(), "image/png")
    composer.add_attachment(one)
    composer.add_attachment(two)
    composer.chips[0].findChild(QToolButton, "removeAttachment").click()
    assert composer.attachments() == [two] and len(composer.chips) == 1
    composer.chips[0].findChild(QToolButton, "removeAttachment").click()
    assert composer.attachments() == [] and composer._strip.isHidden()


def test_clearing_the_composer_clears_the_pictures_too(qapp, composer):
    composer.add_attachment(Attachment("a.png", make_png(), "image/png"))
    composer.set_text("text")
    composer.clear()
    assert composer.text() == "" and composer.attachments() == [] and composer.chips == []


def test_a_pasted_picture_is_attached(qapp, composer):
    image = QImage(80, 60, QImage.Format.Format_RGB32)
    image.fill(QColor("#cc3333"))
    mime = QMimeData()
    mime.setImageData(image)
    assert composer.edit.canInsertFromMimeData(mime)
    composer.edit.insertFromMimeData(mime)
    assert [a.name for a in composer.attachments()] == ["pasted.png"] and composer.attachments()[0].data[:4] == b"\x89PNG"
    assert composer.text() == ""  # not inserted as text


def test_dropped_files_are_attached_and_dropped_text_is_not(qapp, composer, tmp_path):
    first, second = png_file(tmp_path, "a.png"), png_file(tmp_path, "b.png")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(first)), QUrl.fromLocalFile(str(second))])
    assert composer.edit.canInsertFromMimeData(mime)
    composer.edit.insertFromMimeData(mime)
    assert [a.name for a in composer.attachments()] == ["a.png", "b.png"]
    text = QMimeData()
    text.setText("just some words")
    composer.edit.insertFromMimeData(text)
    assert composer.text() == "just some words" and len(composer.attachments()) == 2
    web = QMimeData()
    web.setUrls([QUrl("https://example.com/x.png")])
    web.setText("https://example.com/x.png")
    composer.edit.insertFromMimeData(web)  # a link, not a file: it is text
    assert "example.com" in composer.text() and len(composer.attachments()) == 2


def test_files_that_are_not_pictures_are_refused_with_a_reason(qapp, composer, tmp_path):
    (tmp_path / "notes.png").write_text("this is text")
    composer._on_pictures_given([str(tmp_path / "notes.png"), str(tmp_path / "missing.png")])
    assert composer.attachments() == []
    assert "missing.png does not exist" in composer.problem_text  # the last problem is shown
    composer._on_pictures_given([str(tmp_path / "notes.png")])
    assert "not a PNG, JPEG, WebP or GIF image" in composer.problem_text
    composer._problem_timer.timeout.emit()
    assert composer.problem_text == ""


def test_at_most_ten_pictures(qapp, composer):
    data = make_png(20, 20)
    for i in range(MAX_ATTACHMENTS):
        assert composer.add_attachment(Attachment(f"{i}.png", data, "image/png"))
    assert composer.add_attachment(Attachment("eleven.png", data, "image/png")) is False
    assert len(composer.attachments()) == MAX_ATTACHMENTS and "At most 10 pictures" in composer.problem_text


def test_a_model_without_pictures_turns_the_attach_button_off(qapp, composer):
    reason = "Fake Text cannot look at pictures. Choose a model with the (images) mark to send pictures."
    composer.set_images_allowed(False, reason)
    assert not composer.attach_button.isEnabled() and composer.attach_button.toolTip() == reason
    assert composer.add_attachment(Attachment("a.png", make_png(), "image/png")) is False and reason in composer.problem_text
    composer.set_images_allowed(True)
    assert composer.attach_button.isEnabled() and composer.add_attachment(Attachment("a.png", make_png(), "image/png"))


def test_the_attach_button_follows_whether_sending_is_allowed(qapp, composer):
    composer.set_sending_allowed(False, "gone")
    assert not composer.attach_button.isEnabled()
    composer.set_sending_allowed(True)
    assert composer.attach_button.isEnabled()
    composer.set_images_allowed(False, "no")
    composer.set_sending_allowed(True)
    assert not composer.attach_button.isEnabled()


def test_a_cancelled_file_dialog_attaches_nothing(qapp, composer, monkeypatch):
    monkeypatch.setattr(composer_module, "choose_image_files", lambda parent: [])
    composer.attach_button.click()
    assert composer.attachments() == [] and composer.problem_text == ""


# --- sending pictures: the controller ----------------------------------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    from etcode_desktop.core.store import SessionStore
    s = SessionStore(tmp_path / "store.sqlite3")
    yield s
    s.close()


@pytest.fixture
def harness(fake, store, make_client):
    made = []

    def make(**kw):
        fake.auto_reply = True
        h = Harness(fake, store, make_client, fake.add_session(), **kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.controller.close()


def attachments(*names):
    return [Attachment(n, make_png(30 + i, 30), "image/png") for i, n in enumerate(names)]


def test_pictures_are_uploaded_before_the_message_is_sent(harness, fake):
    h = harness()
    fake.replies.append(Reply(text="nice pictures"))
    atts = attachments("a.png", "b.png")
    key = h.state.begin_send("look", (), atts)
    h.controller.send(key, "look", (), atts)
    h.wait_idle()
    uploaded_hashes = [h_ for (_, h_) in fake.images]
    body = fake.requests_to("POST", "/messages$")[0].json
    assert [p["hash"] for p in body["messages"][0]["content"] if p["type"] == "image"] == uploaded_hashes
    assert body["messages"][0]["content"][0] == {"type": "text", "text": "look"}
    kinds = [type(e).__name__ for e in h.events]
    assert kinds.index("ImagesUploaded") < kinds.index("UserSent")
    assert h.texts() == [("user", "look"), ("assistant", "nice pictures")]
    assert len(h.state.model.entries()) == 2  # the pending message and its committed copy are one


def test_the_pending_message_names_its_pictures_once_they_are_uploaded(harness, fake):
    h = harness()
    atts = attachments("a.png")
    key = h.state.begin_send("look", (), atts)
    assert h.state.model.get(key).image_hashes == [] and h.state.model.get(key).attachments == atts
    h.controller.send(key, "look", (), atts)
    assert h.wait(lambda: h.of(c.ImagesUploaded))
    uploaded_hashes = list(h.of(c.ImagesUploaded)[0].hashes)
    assert len(uploaded_hashes) == 1
    h.wait_idle()
    committed = h.state.model.entries()[0]
    assert committed.image_hashes == uploaded_hashes and committed.index == 1


def test_a_picture_that_the_backend_refuses_means_the_message_was_not_sent(harness, fake):
    h = harness()
    fake.fail_next("POST", "/media/images", status=415, code="unsupported_media", message="Supported formats: PNG, JPEG, WebP, GIF")
    atts = attachments("a.png")
    key = h.state.begin_send("look", (), atts)
    h.controller.send(key, "look", (), atts)
    assert h.wait(lambda: h.of(c.UserFailed))
    failed = h.of(c.UserFailed)[0]
    assert not failed.unknown_outcome and "the picture could not be uploaded" in failed.message and "Supported formats" in failed.message
    assert not fake.requests_to("POST", "/messages$")  # nothing was sent
    assert h.state.model.get(key).state == "failed" and h.state.model.get(key).attachments == atts


def test_an_unreachable_backend_while_uploading_is_a_known_failure(harness, fake):
    h = harness()
    fake.fail_next("POST", "/media/images", status=503, times=10)
    atts = attachments("a.png")
    key = h.state.begin_send("look", (), atts)
    h.controller.send(key, "look", (), atts)
    assert h.wait(lambda: h.of(c.UserFailed))
    assert not h.of(c.UserFailed)[0].unknown_outcome  # the message itself never left


def test_a_refused_token_while_uploading_makes_the_session_unavailable(harness, fake):
    h = harness(token="wrong")
    atts = attachments("a.png")
    key = h.state.begin_send("look", (), atts)
    h.controller.send(key, "look", (), atts)
    assert h.wait(lambda: h.of(c.Unavailable))


def test_known_hashes_and_new_pictures_can_go_together(harness, fake, make_client):
    h = harness()
    known = make_client(fake).upload_image(make_png(10, 10), "old.png").hash
    atts = attachments("new.png")
    key = h.state.begin_send("both", (known,), atts)
    h.controller.send(key, "both", (known,), atts)
    h.wait_idle()
    body = fake.requests_to("POST", "/messages$")[0].json
    hashes = [p["hash"] for p in body["messages"][0]["content"] if p["type"] == "image"]
    assert len(hashes) == 2 and hashes[0] == known


# --- sending pictures: the window --------------------------------------------------------------------------------------------------------

@pytest.fixture
def env(qapp, fake, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda parent, title, text, *a, **k: shown.append(text)))
    e = Env(qapp, fake, tmp_path)
    e.errors = shown
    fake.auto_reply = True
    yield e
    e.close()


def open_with_pictures(env, qapp, fake, tmp_path, session=None, tools=None):
    holder = {}
    picture_loader = ImageLoader(tmp_path / "picture-cache", lambda: holder["bridge"].client)
    session = session or fake.add_session()
    window, bridge, store = env.open(image_loader=picture_loader, tools=tools)
    holder["bridge"] = bridge
    assert wait_until(qapp, lambda: session.id in window.sidebar.ids())
    window.sidebar.select(session.id)
    assert wait_until(qapp, lambda: window.pane.chat.state.loaded and bridge.models is not None)
    return window, window.pane, session, picture_loader


def pictures_of(block):
    return block.thumbnails


def test_attached_pictures_are_uploaded_shown_and_never_downloaded_again(env, qapp, fake, tmp_path):
    fake.replies.append(Reply(text="a blue rectangle"))
    window, pane, session, loader = open_with_pictures(env, qapp, fake, tmp_path)
    pane.composer.add_attachment(Attachment("blue.png", make_png(300, 200), "image/png"))
    pane.composer.set_text("what colour?")
    pane.composer.send_button.click()
    assert wait_until(qapp, lambda: len(blocks(pane)) >= 1 and pictures_of(blocks(pane)[0]) and pictures_of(blocks(pane)[0])[0].has_picture())
    assert pane.composer.attachments() == [] and pane.composer._strip.isHidden()  # the box is free for the next message
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    pane.transcript.flush()
    thumbs = pictures_of(blocks(pane)[0])
    assert len(thumbs) == 1 and wait_until(qapp, thumbs[0].has_picture)  # the committed message shows it, too
    assert texts(pane) == [("UserBlock", "what colour?"), ("AssistantBlock", "a blue rectangle")]
    assert media_gets(fake) == []  # the app had the picture all the time
    assert len(fake.images) == 1


def test_a_picture_can_be_sent_without_text(env, qapp, fake, tmp_path):
    fake.replies.append(Reply(text="I see it"))
    window, pane, session, loader = open_with_pictures(env, qapp, fake, tmp_path)
    pane.composer.add_attachment(Attachment("only.png", make_png(), "image/png"))
    pane.composer.send_button.click()
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    assert fake.sessions[session.id].messages[1]["content"][0]["type"] == "image" or fake.sessions[session.id].messages[1]["content"][-1]["type"] == "image"


def test_pictures_in_the_history_are_downloaded_once(env, qapp, fake, tmp_path, make_client):
    client = make_client(fake)
    image_hash = client.upload_image(make_png(400, 300), "old.png").hash
    session = fake.add_session()
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "an old picture"}, {"type": "image", "hash": image_hash}]})
    window, pane, session, loader = open_with_pictures(env, qapp, fake, tmp_path, session)
    pane.transcript.flush()
    thumbs = pictures_of(blocks(pane)[0])
    assert len(thumbs) == 1 and wait_until(qapp, thumbs[0].has_picture)
    assert len(media_gets(fake, image_hash)) == 1
    window.sidebar.select(window.sidebar.ids()[0])  # showing the conversation again
    time.sleep(0.2)
    qapp.processEvents()
    assert len(media_gets(fake, image_hash)) == 1


def test_a_picture_in_a_tool_result_is_shown_in_its_card(env, qapp, fake, tmp_path, make_client):
    png = make_png(200, 150)
    image_hash = make_client(fake).upload_image(png, "shot.png").hash

    def execute(name, arguments, context):
        return [{"type": "image", "hash": image_hash}, {"type": "text", "text": "Image shot.png (image/png, 1 bytes)"}]

    fake.replies.extend([Reply(text="x", start_delay=0.1, tool_calls=[("c1", "view_image", {"path": "shot.png"})]), Reply(text="a screenshot")])
    window, pane, session, loader = open_with_pictures(env, qapp, fake, tmp_path, tools=ToolRunner(ToolPool(2), execute=execute))
    pane.composer.set_text("look at shot.png")
    pane.composer.send_button.click()
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    pane.transcript.flush()
    assistant = [b for b in blocks(pane) if type(b).__name__ == "AssistantBlock"][0]
    row = assistant.card("c1")._pictures
    assert wait_until(qapp, lambda: row.labels and row.labels[0].has_picture())
    assert not row.isHidden()


def test_a_model_without_pictures_does_not_take_them(env, qapp, fake, tmp_path):
    session = fake.add_session(model="fake-text")
    window, pane, session, loader = open_with_pictures(env, qapp, fake, tmp_path, session)
    assert wait_until(qapp, lambda: not pane.composer.attach_button.isEnabled())
    assert "Fake Text cannot look at pictures" in pane.composer.attach_button.toolTip()
    assert pane.composer.add_attachment(Attachment("a.png", make_png(), "image/png")) is False


def test_pictures_attached_before_the_model_is_known_are_checked_when_sending(env, qapp, fake, tmp_path):
    session = fake.add_session(model="fake-text")
    window, pane, session, loader = open_with_pictures(env, qapp, fake, tmp_path, session)
    pane.composer.set_images_allowed(True)  # as if the catalog had not been there yet
    pane.composer.add_attachment(Attachment("a.png", make_png(), "image/png"))
    pane.composer.set_text("look")
    pane._model_takes_images = lambda state: (False, "Fake Text cannot look at pictures.")
    pane.composer.send_button.click()
    assert "cannot look at pictures" in pane.composer.problem_text
    assert fake.requests_to("POST", "/messages$") == [] and len(pane.composer.attachments()) == 1  # nothing was lost


def test_a_failed_upload_can_be_sent_again_and_the_pictures_go_up_again(env, qapp, fake, tmp_path):
    fake.replies.append(Reply(text="got them"))
    window, pane, session, loader = open_with_pictures(env, qapp, fake, tmp_path)
    fake.fail_next("POST", "/media/images", status=503, times=3)  # all three tries
    pane.composer.add_attachment(Attachment("a.png", make_png(), "image/png"))
    pane.composer.set_text("here")
    pane.composer.send_button.click()
    assert wait_until(qapp, lambda: len(blocks(pane)) == 1 and "not sent" in blocks(pane)[0].state_text)
    block = blocks(pane)[0]
    assert "picture could not be uploaded" in block.state_text and not block._actions.isHidden()
    assert len(pictures_of(block)) == 1 and pictures_of(block)[0].has_picture()  # still shown from the bytes
    block.findChild(QPushButton, "retryMessageButton").click()
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    assert texts(pane) == [("UserBlock", "here"), ("AssistantBlock", "got them")] and len(fake.images) == 1


def test_a_refused_message_is_sent_again_without_uploading_the_pictures_again(env, qapp, fake, tmp_path):
    fake.replies.append(Reply(text="ok now"))
    window, pane, session, loader = open_with_pictures(env, qapp, fake, tmp_path)
    fake.fail_next("POST", "/messages$", status=400, code="validation_error", message="no")
    pane.composer.add_attachment(Attachment("a.png", make_png(), "image/png"))
    pane.composer.set_text("here")
    pane.composer.send_button.click()
    assert wait_until(qapp, lambda: len(blocks(pane)) == 1 and "not sent" in blocks(pane)[0].state_text)
    assert len(fake.requests_to("POST", "/media/images")) == 1
    blocks(pane)[0].findChild(QPushButton, "retryMessageButton").click()
    assert wait_until(qapp, lambda: settled(pane, fake, session), 10)
    assert len(fake.requests_to("POST", "/media/images")) == 1  # the pictures were on the backend already: only the message goes again
    assert len(fake.images) == 1 and texts(pane)[-1] == ("AssistantBlock", "ok now")
