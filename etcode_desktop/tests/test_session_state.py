from etcode_desktop.core import controller as c
from etcode_desktop.core.models import Cost, HistoryEntry, PendingToolCall, SessionMeta
from etcode_desktop.core.session_state import (
    NOTICE_CONNECTION, NOTICE_ERROR, NOTICE_LOAD, NOTICE_TOOLS, NOTICE_UNAVAILABLE, TOOLS_NOTICE, SessionState,
)


def meta(status="idle", cost="0", unpriced=0, queued=0, count=1, pending=None):
    return SessionMeta(id="s", model="m", status=status, queued_count=queued, message_count=count,
                       cost=Cost(total=float(cost), total_exact=cost, unpriced_generations=unpriced), pending_tool_calls=pending)


def make():
    state = SessionState()
    state.notified = 0

    def bump():
        state.notified += 1

    state.subscribe(bump)
    return state


def test_a_new_state_is_empty_and_idle():
    state = SessionState()
    assert state.meta is None and state.status is None and not state.busy and state.can_send
    assert state.cost_text() == "" and not state.loaded and state.queued_count == 0


def test_history_loaded_fills_the_transcript_and_the_meta():
    state = make()
    user = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    state.apply(c.HistoryLoaded((HistoryEntry(1, user),), meta(count=2, cost="0.5")))
    assert state.loaded and [e.text for e in state.model.entries()] == ["hi"]
    assert state.cost_total == "0.5" and state.notified == 1


def test_busy_follows_the_status():
    state = SessionState()
    for status, busy in (("idle", False), ("generating", True), ("awaiting_tools", True), ("failed", False)):
        state.apply(c.StatusChanged(meta(status)))
        assert state.busy is busy, status


def test_cost_text_marks_a_lower_bound():
    state = SessionState()
    state.apply(c.StatusChanged(meta(cost="0.0123")))
    assert state.cost_text() == "$0.0123"
    state.apply(c.StatusChanged(meta(cost="0.0123", unpriced=2)))
    assert state.cost_text() == "≥ $0.0123 (2 unpriced)"


def test_usage_updates_the_cost_but_keeps_the_unpriced_count():
    state = SessionState()
    state.apply(c.StatusChanged(meta(cost="0.1", unpriced=1)))
    state.apply(c.UsageReported("0.35", "0.25", "USD"))
    assert state.cost_total == "0.35" and state.cost_text() == "≥ $0.35 (1 unpriced)"
    state.apply(c.UsageReported(None, None, "USD"))  # an unpriced generation: nothing to show
    assert state.cost_total == "0.35"


def test_queued_count_comes_from_the_meta():
    state = SessionState()
    state.apply(c.StatusChanged(meta("generating", queued=2)))
    assert state.queued_count == 2


def test_notices_come_and_go():
    state = make()
    state.apply(c.LoadFailed("could not load"))
    state.apply(c.ConnectionLost("backend unreachable", 5.0))
    state.apply(c.ActionFailed("could not stop"))
    assert set(state.notices) == {NOTICE_LOAD, NOTICE_CONNECTION, NOTICE_ERROR}
    assert state.notices[NOTICE_CONNECTION].retry_in == 5.0
    state.apply(c.ConnectionRestored())
    assert NOTICE_CONNECTION not in state.notices
    state.dismiss(NOTICE_ERROR)
    state.dismiss(NOTICE_ERROR)  # twice is fine
    assert set(state.notices) == {NOTICE_LOAD}
    state.apply(c.HistoryLoaded((), meta()))
    assert not state.notices  # loading worked: the load notice is gone


def test_a_newer_notice_of_a_kind_replaces_the_older():
    state = SessionState()
    state.apply(c.ConnectionLost("first", 1.0))
    state.apply(c.ConnectionLost("second", 2.0))
    assert state.notices[NOTICE_CONNECTION].text == "second" and len(state.notices) == 1


def test_unavailable_blocks_sending():
    state = SessionState()
    state.apply(c.Unavailable("gone", missing=True))
    assert not state.can_send and state.notices[NOTICE_UNAVAILABLE].missing
    state.apply(c.Unavailable("bad token"))
    assert not state.notices[NOTICE_UNAVAILABLE].missing


def test_the_tools_notice_goes_when_the_session_moves_on():
    state = SessionState()
    state.apply(c.StatusChanged(meta("awaiting_tools")))
    state.apply(c.ToolsPending((PendingToolCall("c1", "bash", {}, False),)))
    assert state.notices[NOTICE_TOOLS].text == TOOLS_NOTICE
    state.apply(c.StatusChanged(meta("awaiting_tools")))
    assert NOTICE_TOOLS in state.notices  # still waiting
    state.apply(c.StatusChanged(meta("idle")))
    assert NOTICE_TOOLS not in state.notices


def test_sending_flow_through_the_state():
    state = SessionState()
    key = state.begin_send("hello", ["a" * 64])
    entry = state.model.get(key)
    assert entry.state == "sending" and entry.text == "hello" and entry.image_hashes == ["a" * 64]
    state.apply(c.UserSent(key, None, True))
    assert entry.state == "queued"
    state.apply(c.UserFailed(key, "no luck", True))
    assert entry.state == "unknown" and entry.error == "no luck"


def test_generation_events_without_a_visible_effect_are_accepted():
    state = SessionState()
    for event in (c.GenerationEnded("g", "complete", "stop"), c.GenerationCancelled("g"), c.GenerationFailed("g", {"code": "x"}),
                  c.StoreUpdated()):
        state.apply(event)
    assert state.model.entries() == [] and not state.notices


def test_live_events_build_the_streaming_message():
    state = SessionState()
    state.apply(c.LiveReasoning("g", "hm "))
    state.apply(c.LiveText("g", "Hel"))
    state.apply(c.LiveText("g", "lo"))
    state.apply(c.LiveToolCall("g", "c1", "websearch", {"query": "q"}))
    state.apply(c.LiveToolResult("c1", [{"type": "text", "text": "found"}]))
    entry = state.model.entries()[0]
    assert (entry.text, entry.reasoning) == ("Hello", "hm ") and entry.tool_calls[0].result[0]["text"] == "found"
    state.apply(c.Committed(3, {"role": "assistant", "text": {"type": "text", "text": "Hello"}, "reasoning": {"type": "text", "text": "hm "},
                                "tool_calls": [{"id": "c1", "name": "websearch", "arguments": {"query": "q"}}],
                                "status": "complete", "error": None}, "g"))
    assert len(state.model.entries()) == 1 and state.model.entries()[0].index == 3 and not state.model.entries()[0].is_live


def test_a_message_that_the_backend_does_not_have_becomes_not_sent():
    state = SessionState()
    key = state.begin_send("hello")
    state.apply(c.UserFailed(key, "timed out", True))
    assert state.model.get(key).state == "unknown"
    state.apply(c.UnknownResolved(key))
    entry = state.model.get(key)
    assert entry.state == "failed" and entry.error == "the backend did not receive it"


def test_loading_the_history_clears_a_refusal_and_keeps_unsent_messages():
    state = SessionState()
    key = state.begin_send("not sent yet")
    state.apply(c.Unavailable("The backend rejected the access token (HTTP 401)."))
    state.apply(c.LoadFailed("could not load"))
    assert not state.can_send
    state.apply(c.HistoryLoaded((), meta()))
    assert state.can_send and not state.notices  # the backend answered: it works again
    assert state.model.get(key).state == "sending"  # and the message that was never sent is still there
