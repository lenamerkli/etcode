import pytest

from etcode_desktop.core.models import HistoryEntry
from etcode_desktop.core.transcript import (
    ADDED, COMMITTED, FAILED, QUEUED, RESET, SENDING, UNKNOWN, UPDATED, TranscriptModel,
)


def user(text="hi", *images):
    return {"role": "user", "content": [{"type": "text", "text": text}] + [{"type": "image", "hash": h} for h in images]}


def assistant(text="", reasoning="", calls=(), status="complete", error=None):
    return {"role": "assistant", "text": {"type": "text", "text": text}, "reasoning": {"type": "text", "text": reasoning},
            "tool_calls": [{"id": i, "name": n, "arguments": a} for i, n, a in calls], "status": status, "error": error}


def tool(call_id, text):
    return {"role": "tool", "tool_call_id": call_id, "content": [{"type": "text", "text": text}]}


SYSTEM = {"role": "system", "content": [{"type": "text", "text": "prompt"}]}


@pytest.fixture
def model():
    m = TranscriptModel()
    m.changes = []
    m.subscribe(m.changes.append)
    return m


def kinds(model):
    return [(c.kind, c.key) for c in model.changes]


def shape(model):
    return [(e.role, e.index, getattr(e, "text", None)) for e in model.entries()]


# --- history -------------------------------------------------------------------------------------------------

def test_loading_history_skips_the_system_prompt(model):
    model.load([HistoryEntry(0, SYSTEM), HistoryEntry(1, user("hello")), HistoryEntry(2, assistant("hi there"))])
    assert shape(model) == [("user", 1, "hello"), ("assistant", 2, "hi there")]
    assert model.has_index(0) and model.has_index(2) and not model.has_index(3)
    assert model.changes[0].kind == RESET


def test_entries_are_sorted_by_index_whatever_the_arrival_order(model):
    model.apply_committed(3, assistant("c"))
    model.apply_committed(1, user("a"))
    model.apply_committed(2, assistant("b"))
    assert [e.index for e in model.entries()] == [1, 2, 3]


def test_the_same_index_twice_changes_nothing(model):
    model.apply_committed(1, user("hello"))
    changes = len(model.changes)
    model.apply_committed(1, user("different"))
    assert len(model.changes) == changes and shape(model) == [("user", 1, "hello")]


def test_load_replaces_what_the_backend_has_and_keeps_what_it_does_not(model):
    model.apply_committed(1, user("old"))
    model.live_text("g1", "streaming")
    pending = model.add_pending_user([{"type": "text", "text": "not on the backend yet"}])
    model.load([HistoryEntry(1, user("new"))])
    assert [(e.role, e.index, e.text) for e in model.entries()] == [("user", 1, "new"), ("user", None, "not on the backend yet")]
    assert model.get(pending).state == SENDING  # still there, and its sending goes on


def test_load_matches_pending_messages_with_their_copies_in_the_history(model):
    sending = model.add_pending_user(parts("arrived"))
    unknown = model.add_pending_user(parts("lost"))
    refused = model.add_pending_user(parts("refused"))
    model.mark_failed(unknown, "timed out", unknown=True)
    model.mark_failed(refused, "no", unknown=False)
    model.load([HistoryEntry(1, user("arrived"))])
    assert model.get(sending) is None  # replaced by the committed copy
    assert model.get(unknown).state == UNKNOWN and model.get(refused).state == FAILED
    assert [e.index for e in model.entries()] == [1, None, None]


def test_unknown_messages_that_the_backend_does_not_have_become_not_sent(model):
    key = model.add_pending_user(parts("lost"))
    model.mark_failed(key, "timed out", unknown=True)
    model.resolve_unknown(key, "the backend did not receive it")
    entry = model.get(key)
    assert (entry.state, entry.error) == (FAILED, "the backend did not receive it")
    model.resolve_unknown(key, "again")  # no longer unknown: nothing changes
    assert model.get(key).error == "the backend did not receive it"
    model.resolve_unknown("nope", "x")  # no error


def test_assistant_message_fields(model):
    model.apply_committed(1, assistant("answer", "thoughts", [("c1", "bash", {"command": "ls"})], "error", {"code": "x", "message": "bad"}))
    entry = model.entries()[0]
    assert (entry.text, entry.reasoning, entry.status, entry.error) == ("answer", "thoughts", "error", {"code": "x", "message": "bad"})
    assert [(c.id, c.name, c.arguments) for c in entry.tool_calls] == [("c1", "bash", {"command": "ls"})]
    assert not entry.is_live


def test_user_images_are_listed(model):
    model.apply_committed(1, user("look", "a" * 64, "b" * 64))
    entry = model.entries()[0]
    assert entry.image_hashes == ["a" * 64, "b" * 64] and entry.text == "look"


# --- tool results --------------------------------------------------------------------------------------------------

def test_tool_results_attach_to_their_call_and_are_not_entries(model):
    model.apply_committed(1, user("go"))
    model.apply_committed(2, assistant("", calls=[("c1", "bash", {}), ("c2", "read_file", {})]))
    model.apply_committed(3, tool("c1", "out1"))
    model.apply_committed(4, tool("c2", "out2"))
    entry = model.entries()[1]
    assert [c.result[0]["text"] for c in entry.tool_calls] == ["out1", "out2"]
    assert len(model.entries()) == 2
    assert model.has_index(3) and model.has_index(4)
    assert ("updated", entry.key) in kinds(model)


def test_a_tool_result_that_arrives_before_its_call_is_kept(model):
    model.apply_committed(3, tool("c1", "early"))
    model.apply_committed(2, assistant("", calls=[("c1", "bash", {})]))
    assert model.entries()[0].tool_calls[0].result[0]["text"] == "early"


def test_a_result_for_a_call_that_never_comes_does_no_harm(model):
    model.apply_committed(1, tool("ghost", "x"))
    assert model.entries() == []


# --- streaming -----------------------------------------------------------------------------------------------------------

def test_streaming_builds_one_live_message(model):
    model.live_reasoning("g1", "think ")
    model.live_text("g1", "Hel")
    model.live_text("g1", "lo")
    entry = model.entries()[0]
    assert (entry.text, entry.reasoning, entry.status, entry.is_live, entry.generation_id) == ("Hello", "think ", "streaming", True, "g1")
    assert [c.kind for c in model.changes] == [ADDED, UPDATED, UPDATED, UPDATED]  # one entry, added once


def test_the_final_message_replaces_the_live_one_in_place(model):
    model.live_text("g1", "Hel")
    key = model.entries()[0].key
    model.apply_committed(1, assistant("Hello!", "r"), generation_id="g1")
    entry = model.entries()[0]
    assert entry.key == key and (entry.text, entry.reasoning, entry.index, entry.is_live) == ("Hello!", "r", 1, False)
    assert len(model.entries()) == 1
    assert model.changes[-1].kind == UPDATED  # same place: just an update, no flicker


def test_a_live_message_moves_into_order_when_committed(model):
    model.live_text("g1", "x")
    model.add_pending_user([{"type": "text", "text": "queued"}])  # shown after the live message
    model.apply_committed(1, user("earlier"))
    model.apply_committed(2, assistant("x"), generation_id="g1")
    assert [e.role for e in model.entries()] == ["user", "assistant", "user"]
    assert model.entries()[2].state == SENDING


def test_live_tool_calls_and_results(model):
    model.live_tool_call("g1", "c1", "websearch", {"query": "q"})
    model.live_tool_call("g1", "c1", "websearch", {"query": "q"})  # a replay
    model.set_tool_result("c1", [{"type": "text", "text": "found"}])
    entry = model.entries()[0]
    assert len(entry.tool_calls) == 1 and entry.tool_calls[0].result[0]["text"] == "found"
    model.apply_committed(5, assistant("done", calls=[("c1", "websearch", {"query": "q"})]), generation_id="g1")
    entry = model.entries()[0]
    assert len(entry.tool_calls) == 1 and entry.tool_calls[0].result[0]["text"] == "found"  # the result survives the commit


def test_a_replayed_generation_does_not_duplicate_a_message_that_is_shown(model):
    model.load([HistoryEntry(0, SYSTEM), HistoryEntry(1, user("q")), HistoryEntry(2, assistant("answer"))])
    model.live_text("g1", "ans")  # the stream is replayed from its start after a reconnect
    model.live_text("g1", "wer")
    assert len(model.entries()) == 3
    model.apply_committed(2, assistant("answer"), generation_id="g1")
    assert shape(model) == [("user", 1, "q"), ("assistant", 2, "answer")]


def test_a_cut_off_live_message_is_finished_by_the_history_copy(model):
    model.live_text("g1", "The answer is")
    model.apply_committed(4, assistant("The answer is 42."))  # met in the history, without a generation id
    assert shape(model) == [("assistant", 4, "The answer is 42.")]


def test_an_unrelated_live_message_is_left_alone_by_a_history_message(model):
    model.live_text("g2", "something else entirely")
    model.apply_committed(4, assistant("The answer is 42."))
    assert [(e.index, e.text) for e in model.entries()] == [(4, "The answer is 42."), (None, "something else entirely")]


def test_two_generations_in_a_row(model):
    model.live_text("g1", "first")
    model.apply_committed(1, assistant("first"), generation_id="g1")
    model.live_text("g2", "second")
    model.apply_committed(2, assistant("second"), generation_id="g2")
    assert shape(model) == [("assistant", 1, "first"), ("assistant", 2, "second")]


# --- messages that the user sent ------------------------------------------------------------------------------------------

def parts(text):
    return [{"type": "text", "text": text}]


def test_a_sent_message_is_committed_in_place(model):
    key = model.add_pending_user(parts("hello"))
    entry = model.get(key)
    assert (entry.state, entry.index) == (SENDING, None)
    model.mark_sent(key, 1, queued=False)
    assert (entry.state, entry.index) == (COMMITTED, 1) and model.get(key) is entry
    assert model.has_index(1)


def test_a_queued_message_stays_at_the_end_until_it_is_committed(model):
    model.live_text("g1", "busy")
    key = model.add_pending_user(parts("next question"))
    model.mark_sent(key, None, queued=True)
    assert model.get(key).state == QUEUED
    model.apply_committed(1, user("first question"))
    model.apply_committed(2, assistant("busy"), generation_id="g1")
    assert [e.role for e in model.entries()] == ["user", "assistant", "user"] and model.entries()[2].state == QUEUED
    model.apply_committed(3, user("next question"))  # the backend committed it: the pending copy is replaced
    assert shape(model) == [("user", 1, "first question"), ("assistant", 2, "busy"), ("user", 3, "next question")]
    assert model.get(key) is None


def test_two_identical_queued_messages_are_matched_one_by_one(model):
    first = model.add_pending_user(parts("again"))
    second = model.add_pending_user(parts("again"))
    model.mark_sent(first, None, queued=True)
    model.mark_sent(second, None, queued=True)
    model.apply_committed(5, user("again"))
    assert model.get(first) is None and model.get(second) is not None
    model.apply_committed(6, user("again"))
    assert model.get(second) is None and len(model.entries()) == 2


def test_the_committed_copy_can_arrive_before_the_answer_to_the_send(model):
    key = model.add_pending_user(parts("hello"))
    model.apply_committed(1, user("hello"))  # a history sync was faster than the response
    assert len(model.entries()) == 1 and model.entries()[0].state == COMMITTED
    model.mark_sent(key, 1, queued=False)  # must not add or duplicate anything
    assert len(model.entries()) == 1 and model.entries()[0].index == 1


def test_a_refused_message_is_marked_and_can_be_discarded(model):
    key = model.add_pending_user(parts("hello"))
    model.mark_failed(key, "Unknown model", unknown=False)
    entry = model.get(key)
    assert (entry.state, entry.error) == (FAILED, "Unknown model")
    model.apply_committed(1, user("hello"))  # a refused message is not matched with a committed one
    assert len(model.entries()) == 2
    model.discard(key)
    assert shape(model) == [("user", 1, "hello")]
    model.discard(key)  # no error


def test_a_message_with_an_unknown_outcome_is_replaced_if_it_turns_up(model):
    key = model.add_pending_user(parts("did it arrive?"))
    model.mark_failed(key, "timed out", unknown=True)
    assert model.get(key).state == UNKNOWN
    model.apply_committed(1, user("did it arrive?"))
    assert shape(model) == [("user", 1, "did it arrive?")] and model.get(key) is None


def test_images_must_match_too(model):
    key = model.add_pending_user(parts("look") + [{"type": "image", "hash": "a" * 64}])
    model.mark_sent(key, None, queued=True)
    model.apply_committed(1, user("look"))  # same text, no image: a different message
    assert len(model.entries()) == 2
    model.apply_committed(2, user("look", "a" * 64))
    assert [e.index for e in model.entries()] == [1, 2]


# --- notifications -------------------------------------------------------------------------------------------------------------

def test_subscribers_are_told_what_changed(model):
    model.apply_committed(1, user("a"))
    key = model.entries()[0].key
    assert kinds(model) == [(ADDED, key)]
    model.unsubscribe(model.changes.append)
    model.apply_committed(2, user("b"))
    assert len(model.changes) == 1
    model.unsubscribe(model.changes.append)  # unknown listener: no error


def test_position_and_get(model):
    model.apply_committed(2, assistant("b"))
    model.apply_committed(1, user("a"))
    first, second = model.entries()
    assert (model.position(first.key), model.position(second.key), model.position("nope")) == (0, 1, None)
    assert model.get(second.key) is second and model.get("nope") is None


def test_the_send_answer_wins_when_the_committed_copy_looks_different(model):
    # e.g. the backend stored the text a little differently, so the two were not matched by their content
    key = model.add_pending_user(parts("hello"))
    model.apply_committed(1, user("hello "))
    assert len(model.entries()) == 2  # the pending one and the committed one
    model.mark_sent(key, 1, queued=False)  # the backend says: that committed message is yours
    assert shape(model) == [("user", 1, "hello ")] and model.get(key) is None
