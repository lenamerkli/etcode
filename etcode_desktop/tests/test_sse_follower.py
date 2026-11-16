import threading
import time

import pytest

pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from etcode_desktop.core.errors import AuthError, NotFound, Unreachable
from etcode_desktop.core.sse import (
    OUTCOME_DONE, OUTCOME_NO_GENERATION, OUTCOME_STOPPED, Backoff, EventFollower,
)
from fake_backend import StreamFault

FAST = Backoff(initial=0.01, factor=1.5, maximum=0.05, give_up_after=5.0)


def text_events(count, start=0):
    return [("text_delta", {"text": f"part{i}"}) for i in range(start, start + count)]


DONE = ("done", {"status": "complete", "finish_reason": "stop", "next_generation_id": None})


def setup_generation(fake, count=5, finish=True, **kwargs):
    session = fake.add_session()
    events = text_events(count) + ([DONE] if finish else [])
    gen = fake.start_generation(session, events)
    return session, gen


def follow(client, session, **kwargs):
    kwargs.setdefault("backoff", FAST)
    return EventFollower(client, session.id, **kwargs)


def texts(events):
    return [e.data["text"] for e in events if e.kind == "text_delta"]


def assert_complete_and_unique(events, count):
    assert texts(events) == [f"part{i}" for i in range(count)]
    assert [e.sequence for e in events] == list(range(count + 1))  # 0..count-1 and the done event
    assert events[-1].kind == "done"


# --- the normal path ------------------------------------------------------------------------------------------

def test_follows_a_generation_to_its_done_event(fake, make_client):
    session, gen = setup_generation(fake, 5)
    follower = follow(make_client(fake), session)
    events = list(follower)
    assert_complete_and_unique(events, 5)
    assert follower.outcome == OUTCOME_DONE
    assert follower.last_event.kind == "done" and follower.last_event_id == f"{gen.id}:5"
    assert all(e.generation_id == gen.id for e in events)
    assert fake.stream_connections == 1


def test_events_that_arrive_while_connected(fake, make_client):
    session, gen = setup_generation(fake, 1, finish=False)

    def produce():
        for i in range(1, 6):
            time.sleep(0.02)
            gen.emit("text_delta", {"text": f"part{i}"})
        gen.emit("done", {"status": "complete", "finish_reason": "stop", "next_generation_id": None})

    thread = threading.Thread(target=produce)
    thread.start()
    events = list(follow(make_client(fake), session))
    thread.join()
    assert_complete_and_unique(events, 6)
    assert fake.stream_connections == 1


def test_done_event_exposes_the_next_generation(fake, make_client):
    session = fake.add_session()
    fake.start_generation(session, text_events(1) + [("done", {"status": "tool_calls", "next_generation_id": "g-next"})])
    events = list(follow(make_client(fake), session))
    assert events[-1].next_generation_id == "g-next"


def test_resuming_from_a_known_event_id(fake, make_client):
    session, gen = setup_generation(fake, 5)
    events = list(follow(make_client(fake), session, since=f"{gen.id}:2"))
    assert [e.sequence for e in events] == [3, 4, 5, 6][:len(events)] and texts(events) == ["part3", "part4"]
    assert fake.requests_to("GET", "/stream$")[0].headers["Last-Event-ID"] == f"{gen.id}:2"


def test_no_active_generation(fake, make_client):
    session = fake.add_session()
    follower = follow(make_client(fake), session)
    assert list(follower) == []
    assert follower.outcome == OUTCOME_NO_GENERATION


def test_unicode_line_separators_in_text_survive(fake, make_client):
    session = fake.add_session()
    tricky = "a\u2028b\u0085c\x0bd\x0ce \U0001F600 \u20ac"
    fake.start_generation(session, [("text_delta", {"text": tricky}), DONE])
    events = list(follow(make_client(fake), session))
    assert events[0].data["text"] == tricky


def test_unknown_events_pass_through_and_garbage_is_skipped(fake, make_client):
    session = fake.add_session()
    fake.stream_faults.append(StreamFault(preamble=b"event: broken\ndata: {nope\n\nevent: weird\ndata: [1]\n\n"))
    fake.start_generation(session, [("brand_new_kind", {"x": 1}), DONE])
    events = list(follow(make_client(fake), session))
    assert [e.kind for e in events] == ["brand_new_kind", "done"]


def test_comments_and_keepalives_are_ignored(fake, make_client):
    session, gen = setup_generation(fake, 2, finish=False)
    fake.stream_faults.append(StreamFault(preamble=b": hello\r\n\r\n", keepalive_every=0.01))

    def finish():
        time.sleep(0.15)
        gen.emit("done", {"status": "complete", "finish_reason": "stop", "next_generation_id": None})

    thread = threading.Thread(target=finish)
    thread.start()
    events = list(follow(make_client(fake), session))
    thread.join()
    assert_complete_and_unique(events, 2)


def test_many_events_in_order(fake, make_client):
    session, gen = setup_generation(fake, 1500)
    events = list(follow(make_client(fake), session))
    assert_complete_and_unique(events, 1500)


# --- dropped connections ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("abrupt", [False, True], ids=["clean-end", "broken-connection"])
def test_reconnects_with_last_event_id_after_a_cut(fake, make_client, abrupt):
    session, gen = setup_generation(fake, 6)
    fake.stream_faults.append(StreamFault(cut_after=3, abrupt=abrupt))
    events = list(follow(make_client(fake), session))
    assert_complete_and_unique(events, 6)
    connections = fake.requests_to("GET", "/stream$")
    assert len(connections) == 2
    assert "Last-Event-ID" not in connections[0].headers
    assert connections[1].headers["Last-Event-ID"] == f"{gen.id}:2"


def test_survives_several_cuts_in_a_row(fake, make_client):
    session, gen = setup_generation(fake, 10)
    for _ in range(4):
        fake.stream_faults.append(StreamFault(cut_after=2, abrupt=True))
    events = list(follow(make_client(fake), session))
    assert_complete_and_unique(events, 10)
    assert fake.stream_connections == 5


def test_a_cut_before_the_first_event_replays_from_the_start(fake, make_client):
    session, gen = setup_generation(fake, 3)
    fake.stream_faults.append(StreamFault(cut_after=0, abrupt=True, preamble=b": connected\n\n"))  # headers out, then reset
    events = list(follow(make_client(fake), session))
    assert_complete_and_unique(events, 3)
    assert "Last-Event-ID" not in fake.requests_to("GET", "/stream$")[1].headers


def test_read_timeout_triggers_a_reconnect(fake, make_client):
    session, gen = setup_generation(fake, 5)
    fake.stream_faults.append(StreamFault(hang_after=2, hang_seconds=5.0))
    client = make_client(fake, stream_read_timeout=0.3)
    started = time.monotonic()
    events = list(follow(client, session))
    assert_complete_and_unique(events, 5)
    assert time.monotonic() - started < 3.0
    assert fake.stream_connections == 2


def test_server_errors_on_connect_are_retried(fake, make_client):
    session, gen = setup_generation(fake, 2)
    fake.stream_faults.extend([StreamFault(status=503), StreamFault(status=502), StreamFault(status=500)])
    events = list(follow(make_client(fake), session))
    assert_complete_and_unique(events, 2)
    assert fake.stream_connections == 4


def test_refused_connections_are_retried_until_the_backend_is_back(fake, make_client):
    session, gen = setup_generation(fake, 2)
    client = make_client(fake)
    real_open = client.open_stream
    calls = []

    def flaky(session_id, last_event_id=None):
        calls.append(1)
        if len(calls) <= 3:
            raise Unreachable("cannot connect")
        return real_open(session_id, last_event_id)

    client.open_stream = flaky
    assert_complete_and_unique(list(follow(client, session)), 2)
    assert len(calls) == 4


def test_gives_up_with_unreachable_after_the_deadline(fake, make_client):
    session, gen = setup_generation(fake, 2)
    fake.stream_faults.extend(StreamFault(status=503) for _ in range(1000))
    follower = follow(make_client(fake), session, backoff=Backoff(initial=0.01, maximum=0.02, give_up_after=0.2))
    started = time.monotonic()
    with pytest.raises(Unreachable):
        list(follower)
    assert 0.15 < time.monotonic() - started < 3.0


def test_the_deadline_restarts_after_a_connection_that_worked(fake, make_client):
    # Each connection delivers one event and then breaks. The total time is longer than
    # give_up_after, but no single stretch without a working connection is.
    session, gen = setup_generation(fake, 6)
    for _ in range(10):
        fake.stream_faults.append(StreamFault(cut_after=1, abrupt=True))
    follower = follow(make_client(fake), session, backoff=Backoff(initial=0.08, factor=1.0, maximum=0.08, give_up_after=0.2))
    events = list(follower)
    assert_complete_and_unique(events, 6)


def test_a_server_that_accepts_and_hangs_up_immediately_is_not_retried_forever(fake, make_client):
    session, gen = setup_generation(fake, 2)
    fake.stream_faults.extend(StreamFault(cut_after=0) for _ in range(1000))  # clean, empty answers
    follower = follow(make_client(fake), session, backoff=Backoff(initial=0.01, maximum=0.02, give_up_after=0.2))
    with pytest.raises(Unreachable):
        list(follower)


def test_generation_finished_while_reconnecting(fake, make_client):
    # We saw events but not `done`, and by the time we reconnect nothing is active any more.
    session, gen = setup_generation(fake, 4, finish=False)
    fake.stream_faults.append(StreamFault(cut_after=1))
    follower = follow(make_client(fake), session)
    iterator = iter(follower)
    assert next(iterator).sequence == 0
    session.active = None
    assert list(iterator) == []
    assert follower.outcome == OUTCOME_NO_GENERATION


# --- ids that don't fit --------------------------------------------------------------------------------------------

def test_an_event_id_from_another_generation_means_replay_from_the_start(fake, make_client):
    session, gen = setup_generation(fake, 3)
    follower = follow(make_client(fake), session, since="some-old-generation:7")
    events = list(follower)
    assert_complete_and_unique(events, 3)
    assert fake.requests_to("GET", "/stream$")[0].headers["Last-Event-ID"] == "some-old-generation:7"
    assert "Last-Event-ID" not in fake.requests_to("GET", "/stream$")[1].headers


def test_replayed_events_are_not_delivered_twice(fake, make_client):
    session, gen = setup_generation(fake, 6)
    fake.stream_faults.append(StreamFault(cut_after=3))
    follower = follow(make_client(fake), session)
    iterator = iter(follower)
    seen = [next(iterator) for _ in range(3)]
    follower._last_event_id = "stale:1"  # the next reconnect is refused (400) and has to replay from the start
    seen.extend(iterator)
    assert_complete_and_unique(seen, 6)
    assert fake.stream_connections == 3


def test_unusable_id_twice_in_a_row_is_an_error_not_a_loop(fake, make_client):
    from etcode_desktop.core.errors import BackendError
    session, gen = setup_generation(fake, 2)
    client = make_client(fake)
    def always_invalid(session_id, last_event_id=None):
        raise BackendError(400, "invalid_event_id", "nope")

    client.open_stream = always_invalid
    with pytest.raises(BackendError):
        list(follow(client, session, since="x:1"))


# --- errors that waiting won't fix ------------------------------------------------------------------------------------

def test_wrong_token_is_raised_at_once(fake, make_client):
    session, gen = setup_generation(fake, 2)
    with pytest.raises(AuthError):
        list(follow(make_client(fake, token="wrong"), session))


def test_unknown_session_is_raised_at_once(fake, make_client):
    session, gen = setup_generation(fake, 2)
    session.id = "other"
    with pytest.raises(NotFound):
        list(follow(make_client(fake), session))
    assert fake.stream_connections == 0


# --- stopping ------------------------------------------------------------------------------------------------------------

def test_close_interrupts_a_read_that_is_waiting_for_data(fake, make_client):
    session, gen = setup_generation(fake, 2, finish=False)  # never finishes: the follower waits for more
    follower = follow(make_client(fake), session)
    got, ended = [], threading.Event()

    def consume():
        for event in follower:
            got.append(event)
        ended.set()

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while len(got) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(got) == 2
    started = time.monotonic()
    follower.close()
    assert ended.wait(3.0), "close() did not interrupt the blocked read"
    assert time.monotonic() - started < 2.0
    assert follower.outcome == OUTCOME_STOPPED


def test_close_during_the_backoff_wait(fake, make_client):
    session, gen = setup_generation(fake, 2)
    fake.stream_faults.extend(StreamFault(status=503) for _ in range(100))
    follower = follow(make_client(fake), session, backoff=Backoff(initial=5.0, give_up_after=60))
    ended = threading.Event()

    def consume():
        list(follower)
        ended.set()

    threading.Thread(target=consume, daemon=True).start()
    time.sleep(0.2)
    follower.close()
    assert ended.wait(2.0)
    assert follower.outcome == OUTCOME_STOPPED


def test_an_external_stop_event_is_honoured(fake, make_client):
    session, gen = setup_generation(fake, 2)
    stop = threading.Event()
    stop.set()
    follower = follow(make_client(fake), session, stop=stop)
    assert list(follower) == [] and follower.outcome == OUTCOME_STOPPED
    assert fake.stream_connections == 0


def test_breaking_out_of_the_loop_early_is_fine(fake, make_client):
    session, gen = setup_generation(fake, 50)
    follower = follow(make_client(fake), session)
    for event in follower:
        if event.sequence == 2:
            break
    follower.close()
    assert follower.last_event.sequence == 2 and follower.outcome in (None, OUTCOME_STOPPED)


def test_close_before_iterating(fake, make_client):
    session, gen = setup_generation(fake, 2)
    follower = follow(make_client(fake), session)
    follower.close()
    assert list(follower) == []


def test_the_socket_under_a_stream_can_be_found(fake, make_client):
    # close() relies on shutting this socket down. If an upgrade of requests/urllib3 moves it,
    # this test fails instead of Stop silently becoming slow.
    session, gen = setup_generation(fake, 2, finish=False)
    connection = make_client(fake).open_stream(session.id)
    try:
        assert connection._socket is not None
    finally:
        connection.close()


# --- notifications for a GUI ----------------------------------------------------------------------------------------

def test_interruptions_and_resumption_are_reported(fake, make_client):
    session, gen = setup_generation(fake, 6)
    fake.stream_faults.extend([StreamFault(cut_after=2, abrupt=True), StreamFault(status=503)])
    log = []
    follower = follow(make_client(fake), session,
                      on_interrupted=lambda error, delay: log.append(("interrupted", type(error).__name__)),
                      on_resumed=lambda: log.append(("resumed",)))
    events = list(follower)
    assert_complete_and_unique(events, 6)
    assert log == [("interrupted", "Unreachable"), ("interrupted", "ServerError"), ("resumed",)]  # resumed once, at the end


def test_no_notifications_on_a_clean_stream(fake, make_client):
    session, gen = setup_generation(fake, 3)
    log = []
    list(follow(make_client(fake), session, on_interrupted=lambda *a: log.append("i"), on_resumed=lambda: log.append("r")))
    assert log == []


def test_resumed_is_reported_also_when_the_generation_turns_out_to_be_over(fake, make_client):
    session, gen = setup_generation(fake, 4, finish=False)
    fake.stream_faults.append(StreamFault(cut_after=1))
    log = []
    follower = follow(make_client(fake), session, on_interrupted=lambda *a: log.append("i"), on_resumed=lambda: log.append("r"))
    iterator = iter(follower)
    next(iterator)
    session.active = None
    assert list(iterator) == []
    assert log == ["i", "r"]


# --- retry now -----------------------------------------------------------------------------------------------------------------------------

def test_retry_now_ends_the_wait_for_the_next_attempt(fake, make_client):
    session, gen = setup_generation(fake, 3)
    fake.stream_faults.append(StreamFault(status=503))  # the first attempt fails, then it would wait 30 s
    follower = follow(make_client(fake), session, backoff=Backoff(initial=30.0, maximum=30.0, give_up_after=120))
    events, finished = [], threading.Event()

    def consume():
        events.extend(follower)
        finished.set()

    threading.Thread(target=consume, daemon=True).start()
    deadline = time.monotonic() + 3
    while fake.stream_connections < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    assert not finished.is_set()  # waiting
    started = time.monotonic()
    follower.retry_now()
    assert finished.wait(5) and time.monotonic() - started < 3
    assert_complete_and_unique(events, 3)


def test_retry_now_while_connected_does_nothing_harmful(fake, make_client):
    session, gen = setup_generation(fake, 5)
    follower = follow(make_client(fake), session)
    follower.retry_now()
    assert_complete_and_unique(list(follower), 5)


def test_a_retry_now_skips_one_wait_and_not_the_ones_after_it(fake, make_client):
    # it is a request to stop waiting, not a permanent switch: the second wait below is a real one
    session, gen = setup_generation(fake, 3)
    fake.stream_faults.extend([StreamFault(cut_after=1, abrupt=True), StreamFault(cut_after=2, abrupt=True)])
    follower = follow(make_client(fake), session, backoff=Backoff(initial=0.3, factor=1.0, maximum=0.3, give_up_after=60))
    follower.retry_now()  # made before the first wait: that wait is skipped
    started = time.monotonic()
    assert_complete_and_unique(list(follower), 3)
    assert time.monotonic() - started >= 0.25  # at least one of the two waits really waited


def test_close_wakes_a_waiting_follower_too(fake, make_client):
    session, gen = setup_generation(fake, 3)
    fake.stream_faults.extend(StreamFault(status=503) for _ in range(10))
    follower = follow(make_client(fake), session, backoff=Backoff(initial=30.0, maximum=30.0, give_up_after=120))
    finished = threading.Event()
    threading.Thread(target=lambda: (list(follower), finished.set()), daemon=True).start()
    time.sleep(0.3)
    follower.close()
    assert finished.wait(2) and follower.outcome == OUTCOME_STOPPED
