import pytest

from etcode_desktop.core.errors import ProtocolError
from etcode_desktop.core.models import (
    Cost, HistoryEntry, ModelInfo, SessionList, SessionMeta, StreamEvent, SubmitResult, UploadedImage,
    image_part, parse_time, text_part, tool_message, user_message,
)

SESSION = {
    "id": "s1", "model": "m", "websearch": True, "options": {"temperature": 0.2}, "status": "awaiting_tools",
    "message_count": 4, "queued_count": 1, "active_generation_id": None,
    "cost": {"currency": "USD", "total": 0.0123, "total_exact": "0.0123", "unpriced_generations": 2},
    "created_at": "2026-11-07T10:00:00+00:00", "updated_at": "2026-11-07T10:05:00+00:00",
    "pending_tool_calls": [
        {"id": "c1", "name": "bash", "arguments": {"command": "ls"}, "resolved": False},
        {"id": "c2", "name": "websearch", "arguments": {"query": "x"}, "resolved": False},
        {"id": "c3", "name": "read_file", "arguments": {"path": "a"}, "resolved": True},
    ],
}


def test_session_meta_parses_everything():
    meta = SessionMeta.from_dict(SESSION)
    assert (meta.id, meta.model, meta.status, meta.websearch) == ("s1", "m", "awaiting_tools", True)
    assert meta.options == {"temperature": 0.2}
    assert (meta.message_count, meta.queued_count, meta.active_generation_id) == (4, 1, None)
    assert meta.cost == Cost("USD", 0.0123, "0.0123", 2)
    assert meta.created_at < meta.updated_at
    assert [c.id for c in meta.pending_tool_calls] == ["c1", "c2", "c3"]
    assert meta.title is None


def test_unresolved_client_calls_skip_websearch_and_resolved_ones():
    assert [c.id for c in SessionMeta.from_dict(SESSION).unresolved_client_calls()] == ["c1"]


def test_missing_pending_tool_calls_means_unknown_not_empty():
    old = {k: v for k, v in SESSION.items() if k != "pending_tool_calls"}
    assert SessionMeta.from_dict(old).pending_tool_calls is None
    assert SessionMeta.from_dict(old).unresolved_client_calls() == ()
    assert SessionMeta.from_dict({**SESSION, "pending_tool_calls": []}).pending_tool_calls == ()


def test_session_meta_tolerates_missing_optional_fields():
    meta = SessionMeta.from_dict({"id": "s", "model": "m", "status": "idle"})
    assert meta.cost == Cost() and meta.options == {} and meta.created_at == 0.0 and not meta.websearch


@pytest.mark.parametrize("bad", [
    None, [], "x", {"model": "m", "status": "idle"}, {"id": "s", "status": "idle"}, {"id": "s", "model": "m"},
    {**SESSION, "pending_tool_calls": "no"}, {**SESSION, "pending_tool_calls": [{"name": "bash"}]},
])
def test_session_meta_rejects_garbage(bad):
    with pytest.raises(ProtocolError):
        SessionMeta.from_dict(bad)


def test_session_list_accepts_object_and_bare_list():
    wrapped = SessionList.from_json({"sessions": [SESSION], "total": 7})
    assert wrapped.total == 7 and wrapped.sessions[0].id == "s1"
    assert SessionList.from_json([SESSION]).total == 1
    with pytest.raises(ProtocolError):
        SessionList.from_json({"sessions": "x"})
    with pytest.raises(ProtocolError):
        SessionList.from_json({"total": 1})


def test_session_title_is_read_from_the_list():
    assert SessionMeta.from_dict({**SESSION, "title": "Fix the bug"}).title == "Fix the bug"


def test_parse_time():
    assert parse_time(12) == 12.0
    assert parse_time("2026-11-07T10:00:00+00:00") == parse_time("2026-11-07T10:00:00Z") == parse_time("2026-11-07T10:00:00")
    assert parse_time("2026-11-07T12:00:00+02:00") == parse_time("2026-11-07T10:00:00Z")
    assert parse_time("garbage") == parse_time(None) == parse_time(True) == 0.0


def test_cost_defaults_and_exact_string():
    assert Cost.from_dict(None) == Cost()
    assert Cost.from_dict({"total": 0.5}).total_exact == "0.5"


def test_model_info_keeps_unknown_fields():
    model = ModelInfo.from_dict({"name": "N", "tech_name": "n", "context_length": 5, "supports_images": True,
                                 "provider": "arena", "provider_tech_name": "x", "available": False})
    assert (model.tech_name, model.supports_images, model.available) == ("n", True, False)
    assert model.extra == {"provider_tech_name": "x"}
    assert ModelInfo.from_dict({"name": "N", "tech_name": "n"}).available is True


def test_history_entry():
    assert HistoryEntry.from_dict({"index": 3, "message": {"role": "user"}}) == HistoryEntry(3, {"role": "user"})
    for bad in ({"index": "3", "message": {}}, {"index": True, "message": {}}, {"message": {}}, {"index": 1}, []):
        with pytest.raises(ProtocolError):
            HistoryEntry.from_dict(bad)


def test_submit_result():
    result = SubmitResult.from_dict({"accepted": [{"queue_id": "q", "queued": True}], "generation_id": None, "session": SESSION})
    assert result.queued and result.generation_id is None and result.session.id == "s1"
    started = SubmitResult.from_dict({"accepted": [{"index": 5, "queued": False}], "generation_id": "g", "session": SESSION})
    assert not started.queued and started.generation_id == "g"


def test_uploaded_image():
    assert UploadedImage.from_dict({"hash": "h", "media_type": "image", "mime_type": "image/png"}) == UploadedImage("h", "image", "image/png")


def test_stream_event_next_generation():
    done = StreamEvent("done", {"next_generation_id": "g2"}, "g:1", "g", 1)
    assert done.is_done and done.next_generation_id == "g2"
    assert StreamEvent("text_delta", {"next_generation_id": "x"}, None, None, None).next_generation_id is None


def test_message_builders():
    assert user_message("hi") == {"role": "user", "content": [text_part("hi")]}
    assert user_message("hi", ["a" * 64]) == {"role": "user", "content": [text_part("hi"), image_part("a" * 64)]}
    assert user_message("", ["a" * 64]) == {"role": "user", "content": [image_part("a" * 64)]}
    assert user_message("") == {"role": "user", "content": [text_part("")]}
    assert tool_message("c1", [text_part("out")]) == {"role": "tool", "tool_call_id": "c1", "content": [text_part("out")]}
    with pytest.raises(ValueError):
        tool_message("c1", [])
