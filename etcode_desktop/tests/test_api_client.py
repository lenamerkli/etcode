import threading

import pytest
import requests

pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from etcode_desktop.core.api import BackendClient, RetryPolicy, Timeouts, error_from_response
from etcode_desktop.core.errors import (
    AuthError, BackendError, Conflict, NotFound, ProtocolError, ServerError, Unreachable, Unsupported,
)
from etcode_desktop.core.models import SessionMeta, text_part, tool_message, user_message
from fake_backend import PNG


# --- basics --------------------------------------------------------------------------------------

def test_every_request_carries_the_token_and_a_user_agent(fake, make_client):
    make_client(fake).models()
    request = fake.requests[-1]
    assert request.headers["Authorization"] == "Bearer tok-a"
    assert request.headers["User-Agent"].startswith("etcode-desktop/")


def test_models(fake, make_client):
    models = make_client(fake).models()
    assert [m.tech_name for m in models] == ["fake-coder", "fake-coder-lite", "fake-text"]
    assert models[0].supports_images and models[1].supports_images and not models[2].supports_images
    assert all(m.available for m in models)


def test_create_session_sends_only_the_options_that_were_given(fake, make_client):
    client = make_client(fake)
    meta = client.create_session("fake-coder")
    assert isinstance(meta, SessionMeta) and meta.status == "idle" and meta.model == "fake-coder"
    assert fake.requests[-1].json == {"model": "fake-coder", "websearch": False}
    client.create_session("fake-text", websearch=True, temperature=0.5, max_tokens=100, reasoning_effort="low")
    assert fake.requests[-1].json == {"model": "fake-text", "websearch": True, "temperature": 0.5,
                                      "max_tokens": 100, "reasoning_effort": "low"}


def test_get_session_and_ownership(fake, make_client):
    session = fake.add_session(owner="tok-a")
    assert make_client(fake).get_session(session.id).id == session.id
    with pytest.raises(NotFound) as info:
        make_client(fake, token="tok-b").get_session(session.id)  # another token's session
    assert info.value.code == "session_not_found" and info.value.status == 404
    with pytest.raises(NotFound):
        make_client(fake).get_session("does-not-exist")


def test_session_ids_are_url_quoted(fake, make_client):
    with pytest.raises(NotFound):
        make_client(fake).get_session("a/b?c")
    assert fake.requests[-1].raw_path == "/session/a%2Fb%3Fc"


def test_set_model(fake, make_client):
    session = fake.add_session()
    assert make_client(fake).set_model(session.id, "fake-coder-lite").model == "fake-coder-lite"
    with pytest.raises(Conflict) as incompatible:  # from a model with pictures to one without
        make_client(fake).set_model(session.id, "fake-text")
    assert incompatible.value.code == "incompatible_model"
    with pytest.raises(BackendError) as info:
        make_client(fake).set_model(session.id, "nope")
    assert info.value.code == "unknown_model" and info.value.status == 400
    session.status = "generating"
    with pytest.raises(Conflict) as busy:
        make_client(fake).set_model(session.id, "fake-coder")
    assert busy.value.code == "session_busy"


def test_history(fake, make_client):
    session = fake.add_session()
    client = make_client(fake)
    client.submit(session.id, [user_message("one")])
    client.submit(session.id, [user_message("two")])
    full = client.messages(session.id)
    assert [e.index for e in full] == [0, 1, 2] and full[0].message["role"] == "system"
    assert full[2].message["content"] == [text_part("two")]
    assert [e.index for e in client.last(session.id, 2)] == [1, 2]
    with pytest.raises(BackendError) as info:
        client.last(session.id, 5000)
    assert info.value.code == "invalid_number"


# --- sessions list -----------------------------------------------------------------------------------

def test_list_sessions(fake, make_client):
    first = fake.add_session(owner="tok-a")
    second = fake.add_session(owner="tok-a")
    fake.add_session(owner="tok-b")
    client = make_client(fake)
    client.submit(first.id, [user_message("hello there")])  # touches `first`, so it sorts first
    listing = client.list_sessions()
    assert listing.total == 2
    assert [s.id for s in listing.sessions] == [first.id, second.id]
    assert listing.sessions[0].title == "hello there" and listing.sessions[1].title is None
    assert [s.id for s in client.list_sessions(limit=1, offset=1).sessions] == [second.id]
    assert fake.requests[-1].args == {"limit": "1", "offset": "1"}


def test_list_sessions_paging_errors(fake, make_client):
    client = make_client(fake)
    for kwargs in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        with pytest.raises(BackendError) as info:
            client.list_sessions(**kwargs)
        assert (info.value.status, info.value.code) == (400, "invalid_number")
        assert type(info.value) is BackendError


def test_list_sessions_on_a_backend_without_the_endpoint(legacy_fake, make_client):
    client = make_client(legacy_fake)
    with pytest.raises(Unsupported):
        client.list_sessions()
    assert len(legacy_fake.requests_to("GET", "^/sessions$")) == 1  # 404 is not retried


def test_old_backend_sessions_have_no_pending_tool_calls_field(legacy_fake, make_client):
    session = legacy_fake.add_session()
    assert make_client(legacy_fake).get_session(session.id).pending_tool_calls is None


def test_pending_tool_calls_are_reported(fake, make_client):
    session = fake.add_session()
    session.status = "awaiting_tools"
    session.pending = [{"id": "c1", "name": "bash", "arguments": {"command": "ls"}},
                       {"id": "c2", "name": "websearch", "arguments": {"query": "q"}, "result": [text_part("r")]}]
    meta = make_client(fake).get_session(session.id)
    assert [(c.id, c.resolved, c.backend) for c in meta.pending_tool_calls] == [("c1", False, False), ("c2", True, True)]
    assert meta.pending_tool_calls[0].arguments == {"command": "ls"}


def test_run_backend_tool_call(fake, make_client):
    session = fake.add_session(websearch=True)
    session.status = "awaiting_tools"
    session.pending = [{"id": "w", "name": "websearch", "arguments": {"query": "q"}, "search_result": "1. result"}]
    result = make_client(fake).run_tool_call(session.id, "w")
    assert result.call_id == "w" and result.content == [text_part("1. result")]
    assert result.duplicate is False and result.session.pending_tool_calls == ()
    assert fake.requests[-1].raw_path == f"/session/{session.id}/tool-calls/w/run"


# --- submit and cancel -------------------------------------------------------------------------------------

def test_submit_user_message(fake, make_client):
    session = fake.add_session()
    result = make_client(fake).submit(session.id, [user_message("hi")], temperature=0.1, max_tokens=7, reasoning_effort="high")
    assert not result.queued and result.accepted[0]["index"] == 1 and result.generation_id is None
    assert fake.requests[-1].path == f"/session/{session.id}/messages"
    assert fake.requests[-1].json == {"messages": [user_message("hi")], "temperature": 0.1, "max_tokens": 7,
                                      "reasoning_effort": "high"}


def test_submit_while_busy_is_queued(fake, make_client):
    session = fake.add_session()
    fake.start_generation(session)
    result = make_client(fake).submit(session.id, [user_message("later")])
    assert result.queued and "index" not in result.accepted[0]
    assert result.generation_id == session.active.id and result.session.queued_count == 1


def test_tool_results_accepted_duplicate_and_conflicts(fake, make_client):
    session = fake.add_session()
    session.status = "awaiting_tools"
    session.pending = [{"id": "c1", "name": "bash", "arguments": {}}, {"id": "c2", "name": "bash", "arguments": {}},
                       {"id": "w", "name": "websearch", "arguments": {}}]
    client = make_client(fake)
    out = [text_part("out")]
    first = client.submit(session.id, [tool_message("c1", out)])
    assert first.accepted == ({"tool_call_id": "c1", "accepted": True},)
    assert first.session.status == "awaiting_tools"
    again = client.submit(session.id, [tool_message("c1", out)])
    assert again.accepted == ({"tool_call_id": "c1", "duplicate": True},)
    with pytest.raises(Conflict) as differ:
        client.submit(session.id, [tool_message("c1", [text_part("other")])])
    assert differ.value.code == "duplicate_tool_result"
    with pytest.raises(Conflict) as backend_tool:
        client.submit(session.id, [tool_message("w", out)])
    assert backend_tool.value.code == "backend_tool"
    with pytest.raises(Conflict) as unknown:
        client.submit(session.id, [tool_message("zzz", out)])
    assert unknown.value.code == "unknown_tool_call"


def test_cancel(fake, make_client):
    session = fake.add_session()
    client = make_client(fake)
    with pytest.raises(Conflict) as nothing:
        client.cancel(session.id)
    assert nothing.value.code == "no_generation"
    gen = fake.start_generation(session)
    assert client.cancel(session.id) == {"cancelled": "generation", "generation_id": gen.id}
    assert gen.cancel_requested


# --- images ----------------------------------------------------------------------------------------------------

def test_image_upload_and_download_round_trip(fake, make_client):
    client = make_client(fake)
    uploaded = client.upload_image(PNG, "pic.png")
    assert uploaded.mime_type == "image/png" and len(uploaded.hash) == 64 and uploaded.media_type == "image"
    assert client.upload_image(PNG).hash == uploaded.hash  # same content, same hash
    downloaded = client.download_image(uploaded.hash)
    assert downloaded.data == PNG and downloaded.mime_type == "image/png"


def test_image_errors(fake, make_client):
    client = make_client(fake)
    with pytest.raises(BackendError) as bad:
        client.upload_image(b"this is not an image")
    assert (bad.value.status, bad.value.code) == (415, "unsupported_media")
    with pytest.raises(BackendError) as empty:
        client.upload_image(b"")
    assert empty.value.status == 413
    with pytest.raises(NotFound) as missing:
        client.download_image("0" * 64)
    assert missing.value.code == "image_not_found"
    uploaded = client.upload_image(PNG)
    with pytest.raises(NotFound):
        make_client(fake, token="tok-b").download_image(uploaded.hash)  # images belong to the uploader's token
    for bad_hash in ("short", "G" * 64, "../etc/passwd", "A" * 64):
        with pytest.raises(ValueError):
            client.download_image(bad_hash)


# --- errors --------------------------------------------------------------------------------------------------------

def test_wrong_token_is_an_auth_error(fake, make_client):
    with pytest.raises(AuthError) as info:
        make_client(fake, token="wrong").models()
    assert (info.value.status, info.value.code) == (401, "unauthorized")
    assert "wrong" not in str(info.value)


def test_missing_token_is_an_auth_error(fake, make_client):
    with pytest.raises(AuthError):
        make_client(fake, token="").models()


def test_other_error_statuses_map_to_the_right_classes(fake, make_client):
    client = make_client(fake, retry=RetryPolicy(attempts=1))
    for status, cls in ((400, BackendError), (403, BackendError), (413, BackendError), (500, ServerError), (501, ServerError)):
        fake.fail_next("GET", "^/models$", status=status, code="boom", message="it broke")
        with pytest.raises(cls) as info:
            client.models()
        assert type(info.value) is cls
        assert (info.value.status, info.value.code, info.value.message) == (status, "boom", "it broke")
        assert str(info.value) == f"it broke (boom, HTTP {status})"


def test_non_json_error_body_gets_a_synthetic_code(fake, make_client):
    fake.fail_next("GET", "^/models$", status=502, body="<html><body>Bad gateway from the proxy</body></html>")
    with pytest.raises(ServerError) as info:
        make_client(fake, retry=RetryPolicy(attempts=1)).models()
    assert info.value.code == "http_502" and "Bad gateway from the proxy" in info.value.message


def test_error_from_response_without_body():
    response = requests.Response()
    response.status_code, response.reason, response._content = 418, "I'm a teapot", b""
    err = error_from_response(response)
    assert (err.status, err.code, err.message) == (418, "http_418", "I'm a teapot")


def test_invalid_json_in_a_success_response(fake, make_client):
    fake.app.add_url_rule("/garbage", "garbage", lambda: ("not json", 200))
    client = make_client(fake)
    with pytest.raises(ProtocolError):
        client._json(client._call("GET", "/garbage"))


def test_wrong_shape_in_a_success_response(fake, make_client):
    fake.app.add_url_rule("/models-broken", "models_broken", lambda: ({"not": "a list"}, 200))
    client = make_client(fake)
    original = client._call
    client._call = lambda method, path, **kw: original(method, "/models-broken" if path == "/models" else path, **kw)
    with pytest.raises(ProtocolError):
        client.models()


def test_unreachable_when_nothing_listens(make_client):
    client = make_client("http://127.0.0.1:9", retry=RetryPolicy(attempts=1))
    with pytest.raises(Unreachable) as info:
        client.models()
    assert "127.0.0.1:9" in str(info.value)


def test_unreachable_on_read_timeout(fake, make_client):
    client = make_client(fake, timeouts=Timeouts(connect=1.0, read=0.2), retry=RetryPolicy(attempts=1))
    fake.fail_next("GET", "^/models$", delay=1.0)
    with pytest.raises(Unreachable) as info:
        client.models()
    assert "did not answer in time" in str(info.value)


# --- retries --------------------------------------------------------------------------------------------------------

def test_get_is_retried_on_503_and_then_succeeds(fake, make_client):
    delays = []
    client = make_client(fake, sleep=delays.append, retry=RetryPolicy(attempts=3, base_delay=0.5, max_delay=4.0))
    fake.fail_next("GET", "^/models$", status=503, times=2)
    assert len(client.models()) == 3
    assert len(fake.requests_to("GET", "^/models$")) == 3
    assert delays == [0.5, 1.0]  # exponential backoff


def test_get_gives_up_after_the_attempts_and_raises_the_last_error(fake, make_client):
    client = make_client(fake)
    fake.fail_next("GET", "^/models$", status=503, code="busy", times=10)
    with pytest.raises(ServerError) as info:
        client.models()
    assert info.value.code == "busy" and len(fake.requests_to("GET", "^/models$")) == 3


@pytest.mark.parametrize("status", [400, 401, 404, 409, 500, 501])
def test_other_statuses_are_not_retried(fake, make_client, status):
    fake.fail_next("GET", "^/models$", status=status, times=10)
    with pytest.raises(BackendError):
        make_client(fake).models()
    assert len(fake.requests_to("GET", "^/models$")) == 1


def test_get_is_retried_after_a_timeout(fake, make_client):
    client = make_client(fake, timeouts=Timeouts(connect=1.0, read=0.2))
    fake.fail_next("GET", "^/models$", delay=0.6, times=1)
    assert len(client.models()) == 3


def test_backoff_delay_is_capped(fake, make_client):
    delays = []
    client = make_client(fake, sleep=delays.append, retry=RetryPolicy(attempts=5, base_delay=1.0, max_delay=3.0))
    fake.fail_next("GET", "^/models$", status=503, times=10)
    with pytest.raises(ServerError):
        client.models()
    assert delays == [1.0, 2.0, 3.0, 3.0]


def test_a_user_message_is_never_retried(fake, make_client):
    session = fake.add_session()
    client = make_client(fake)
    fake.fail_next("POST", "/messages$", status=503, times=5)
    with pytest.raises(ServerError):
        client.submit(session.id, [user_message("hi")])
    assert len(fake.requests_to("POST", "/messages$")) == 1
    fake.rest_faults.clear()
    fake.fail_next("POST", "/messages$", delay=0.6, times=1)
    short = make_client(fake, timeouts=Timeouts(connect=1.0, read=0.2))
    with pytest.raises(Unreachable):  # outcome unknown: the caller must decide, we must not resend
        short.submit(session.id, [user_message("hi again")])
    assert len(fake.requests_to("POST", "/messages$")) == 2


def test_session_creation_is_not_retried(fake, make_client):
    fake.fail_next("POST", "^/session$", status=503, times=5)
    with pytest.raises(ServerError):
        make_client(fake).create_session("fake-coder")
    assert len(fake.requests_to("POST", "^/session$")) == 1 and not fake.sessions


def test_tool_results_are_retried(fake, make_client):
    session = fake.add_session()
    session.status = "awaiting_tools"
    session.pending = [{"id": "c1", "name": "bash", "arguments": {}}]
    client = make_client(fake)
    fake.fail_next("POST", "/messages$", status=503, times=2)
    result = client.submit(session.id, [tool_message("c1", [text_part("ok")])])
    assert result.accepted == ({"tool_call_id": "c1", "accepted": True},)
    assert len(fake.requests_to("POST", "/messages$")) == 3


def test_a_retried_tool_result_whose_first_try_got_through_is_a_duplicate(fake, make_client):
    session = fake.add_session()
    session.status = "awaiting_tools"
    session.pending = [{"id": "c1", "name": "bash", "arguments": {}}, {"id": "c2", "name": "bash", "arguments": {}}]
    client = make_client(fake, timeouts=Timeouts(connect=1.0, read=0.2))
    # first try: processed by the server, but the answer is lost to a client timeout
    original = client._send
    calls = []

    def flaky(method, path, **kw):
        calls.append(1)
        response = original(method, path, **kw)
        if len(calls) == 1:
            raise Unreachable("answer lost")
        return response

    client._send = flaky
    result = client.submit(session.id, [tool_message("c1", [text_part("ok")])])
    assert result.accepted == ({"tool_call_id": "c1", "duplicate": True},)


def test_image_upload_is_retried(fake, make_client):
    fake.fail_next("POST", "/media/images", status=502, times=1)
    assert make_client(fake).upload_image(PNG).mime_type == "image/png"


# --- plumbing -----------------------------------------------------------------------------------------------------------

def test_each_thread_gets_its_own_http_session(fake, make_client):
    client = make_client(fake)
    seen = {}

    def work(name):
        client.models()
        seen[name] = client._session()

    threads = [threading.Thread(target=work, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    client.models()
    assert len({id(s) for s in seen.values()} | {id(client._session())}) == 4


def test_concurrent_requests(fake, make_client):
    client = make_client(fake)
    errors = []

    def work():
        try:
            for _ in range(10):
                client.models()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(fake.requests_to("GET", "^/models$")) == 60


def test_the_token_is_not_in_repr_or_error_text(fake, make_client):
    client = make_client(fake, token="tok-a")
    assert "tok-a" not in repr(client) and "<set>" in repr(client)
    with pytest.raises(NotFound) as info:
        client.get_session("nope")
    assert "tok-a" not in str(info.value)


def test_base_url_with_a_path_prefix_and_trailing_slash():
    client = BackendClient("https://host.example/etcode/", "t-1234")
    assert client._url("/models") == "https://host.example/etcode/models"
    client.close()


def test_close_is_idempotent_and_the_client_is_usable_afterwards(fake, make_client):
    client = make_client(fake)
    client.models()
    client.close()
    client.close()
    assert len(client.models()) == 3


def test_from_settings(fake):
    from etcode_desktop.config import Settings
    client = BackendClient.from_settings(Settings(backend_url=fake.url + "/", token="tok-a"))
    assert client.base_url == fake.url and len(client.models()) == 3
    client.close()


# --- StreamConnection ----------------------------------------------------------------------------------------------------

class _BrokenResponse:
    """A response whose reader fails the way urllib3 does when the response is closed underneath it."""
    status_code = 200
    raw = None

    def iter_content(self, chunk_size=None):
        yield b"data: x\n\n"
        raise AttributeError("'NoneType' object has no attribute 'readline'")

    def close(self):
        pass


def test_a_failure_caused_by_our_own_close_is_a_lost_connection():
    from etcode_desktop.core.api import StreamConnection
    connection = StreamConnection(_BrokenResponse(), "http://x")
    chunks = connection.chunks()
    assert next(chunks) == b"data: x\n\n"
    connection.close()
    with pytest.raises(Unreachable):
        next(chunks)


def test_an_unexpected_failure_without_close_is_not_swallowed():
    from etcode_desktop.core.api import StreamConnection
    chunks = StreamConnection(_BrokenResponse(), "http://x").chunks()
    next(chunks)
    with pytest.raises(AttributeError):
        next(chunks)


def test_a_closed_connection_yields_nothing():
    from etcode_desktop.core.api import StreamConnection
    connection = StreamConnection(_BrokenResponse(), "http://x")
    connection.close()
    assert list(connection.chunks()) == []


# --- the fake backend can go down and come back (the tests of outages rely on it) ---------------------------------------------------

def test_a_backend_that_is_down_refuses_and_comes_back_with_its_state(fake, make_client):
    from etcode_desktop.core.errors import Unreachable
    session = fake.add_session()
    client = make_client(fake, retry=RetryPolicy(attempts=1))
    assert client.get_session(session.id).id == session.id
    fake.go_down()
    with pytest.raises(Unreachable):
        client.get_session(session.id)  # also over the connection that was kept alive
    with pytest.raises(Unreachable):
        client.models()
    fake.come_back()
    assert client.get_session(session.id).id == session.id  # the same session, at the same address
    fake.go_down()
    fake.go_down()  # twice is fine
    fake.come_back()
    fake.come_back()
    assert len(client.models()) == 3
