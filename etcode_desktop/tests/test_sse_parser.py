import json

import pytest

from etcode_desktop.core.sse import RawEvent, decode_event, parse_event_id, parse_events, split_lines


def lines_of(data: bytes, size: int | None = None):
    chunks = [data] if size is None else [data[i:i + size] for i in range(0, len(data), size)]
    return list(split_lines(chunks))


# --- split_lines --------------------------------------------------------------------------

def test_split_on_all_three_line_endings():
    assert lines_of(b"a\nb\r\nc\rd\n") == ["a", "b", "c", "d"]


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 100])
def test_chunk_boundaries_make_no_difference(size):
    data = "id: x:1\r\nevent: text_delta\r\ndata: h\u00e9llo \U0001F600\r\n\r\n: ping\n\nlast".encode()
    assert lines_of(data, size) == lines_of(data)


def test_crlf_split_between_chunks_is_one_line_ending():
    assert list(split_lines([b"a\r", b"\nb\r\n"])) == ["a", "b"]
    assert list(split_lines([b"a\r", b"b\n"])) == ["a", "b"]  # a lone \r followed by text
    assert list(split_lines([b"a\r", b"\r\n", b"b\n"])) == ["a", "", "b"]


def test_unicode_line_separators_do_not_split_lines():
    payload = "data: x\u2028y\u0085z\x0bw\x0cv\x1cu\n"
    assert lines_of(payload.encode()) == ["data: x\u2028y\u0085z\x0bw\x0cv\x1cu"]
    assert lines_of(payload.encode(), 1) == ["data: x\u2028y\u0085z\x0bw\x0cv\x1cu"]


def test_multibyte_character_split_across_chunks():
    data = "data: \u20ac\n".encode()  # the euro sign is three bytes
    assert lines_of(data, 1) == ["data: \u20ac"]


def test_incomplete_last_line_is_still_returned_and_invalid_utf8_is_replaced():
    assert lines_of(b"abc") == ["abc"]
    assert lines_of(b"a\xffb\n") == ["a\ufffdb"]
    assert lines_of(b"") == [] and list(split_lines([b"", b""])) == []


# --- parse_events ------------------------------------------------------------------------

def events_of(text: str):
    return list(parse_events(text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")))


def test_basic_event():
    assert events_of("id: g:0\nevent: text_delta\ndata: {\"text\": \"hi\"}\n\n") == [
        RawEvent("text_delta", '{"text": "hi"}', "g:0")]


def test_default_event_name_and_missing_id():
    assert events_of("data: x\n\n") == [RawEvent("message", "x", None)]


def test_multiple_data_lines_are_joined_with_newlines():
    assert events_of("event: e\ndata: a\ndata: b\ndata\n\n") == [RawEvent("e", "a\nb\n", None)]


def test_only_one_leading_space_is_removed():
    assert events_of("data:x\n\n")[0].data == "x"
    assert events_of("data:  x\n\n")[0].data == " x"


def test_comments_and_unknown_fields_are_ignored():
    text = ": keepalive\nretry: 100\nfoo: bar\nid: g:1\nevent: e\n: another\ndata: d\n\n"
    assert events_of(text) == [RawEvent("e", "d", "g:1")]


def test_event_without_data_is_not_dispatched_and_does_not_leak():
    assert events_of("event: lonely\nid: g:1\n\ndata: x\n\n") == [RawEvent("message", "x", None)]


def test_unterminated_event_is_dropped():
    assert events_of("event: e\ndata: cut off") == []
    assert events_of("data: a\n\ndata: cut off") == [RawEvent("message", "a", None)]


def test_id_with_nul_is_ignored():
    assert events_of("id: a\0b\ndata: x\n\n") == [RawEvent("message", "x", None)]


def test_consecutive_blank_lines():
    assert events_of("data: a\n\n\n\ndata: b\n\n") == [RawEvent("message", "a", None), RawEvent("message", "b", None)]


# --- ids and decoding -----------------------------------------------------------------------

def test_parse_event_id():
    assert parse_event_id("abc:12") == ("abc", 12)
    assert parse_event_id("a:b:3") == ("a:b", 3)
    for bad in (None, "", "abc", ":3", "abc:", "abc:x", "abc:-1"):
        assert parse_event_id(bad) is None


def test_decode_event_prefers_payload_fields_and_falls_back_to_the_id():
    event = decode_event(RawEvent("text_delta", json.dumps({"generation_id": "g", "sequence": 4, "text": "x"}), "g:4"))
    assert (event.kind, event.generation_id, event.sequence, event.data["text"], event.event_id) == ("text_delta", "g", 4, "x", "g:4")
    fallback = decode_event(RawEvent("done", json.dumps({"status": "complete"}), "g:9"))
    assert (fallback.generation_id, fallback.sequence) == ("g", 9)
    bare = decode_event(RawEvent("done", "{}", None))
    assert (bare.generation_id, bare.sequence, bare.event_id) == (None, None, None)


def test_decode_event_rejects_unusable_payloads():
    assert decode_event(RawEvent("e", "{not json", None)) is None
    assert decode_event(RawEvent("e", "[1, 2]", None)) is None
    assert decode_event(RawEvent("e", '"text"', None)) is None


def test_booleans_are_not_sequence_numbers():
    event = decode_event(RawEvent("e", json.dumps({"generation_id": "g", "sequence": True}), None))
    assert event.sequence is None
