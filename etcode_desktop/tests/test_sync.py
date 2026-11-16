import pytest

pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from etcode_desktop.core.errors import AuthError, NotFound, ServerError, Unreachable
from etcode_desktop.core.models import user_message
from etcode_desktop.core.store import Scope, SessionStore, StoreError
from etcode_desktop.core.sync import SessionSync


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "store.sqlite3")
    yield s
    s.close()


def make_sync(fake, store, make_client, token="tok-a", **kwargs):
    client = make_client(fake, token=token)
    scope = Scope.for_token(fake.url, token)
    return SessionSync(client, store, scope, **kwargs), scope, client


# --- sync with a backend that lists sessions -------------------------------------------------------------------

def test_sync_imports_the_backends_sessions(fake, store, make_client):
    first, second = fake.add_session(), fake.add_session()
    sync, scope, client = make_sync(fake, store, make_client)
    client.submit(first.id, [user_message("Fix the tests")])
    result = sync.sync()
    assert (result.supported, result.complete, result.imported, result.updated) == (True, True, 2, 0)
    records = {r.id: r for r in store.list(scope)}
    assert set(records) == {first.id, second.id}
    assert records[first.id].title == "Fix the tests" and records[second.id].title == ""
    assert records[first.id].model == "fake-coder" and records[first.id].status == "idle"
    assert records[first.id].created_at > 1e9


def test_sync_updates_known_sessions(fake, store, make_client):
    session = fake.add_session()
    sync, scope, client = make_sync(fake, store, make_client)
    sync.sync()
    session.status = "generating"
    session.model = "fake-text"
    session.cost_exact = "0.25"
    result = sync.sync()
    assert (result.imported, result.updated) == (0, 1)
    record = store.get(scope, session.id)
    assert (record.status, record.model, record.cost_total) == ("generating", "fake-text", "0.25")


def test_sync_only_sees_the_sessions_of_its_own_token(fake, store, make_client):
    mine = fake.add_session(owner="tok-a")
    theirs = fake.add_session(owner="tok-b")
    sync_a, scope_a, _ = make_sync(fake, store, make_client, token="tok-a")
    sync_b, scope_b, _ = make_sync(fake, store, make_client, token="tok-b")
    sync_a.sync()
    sync_b.sync()
    assert store.ids(scope_a) == {mine.id} and store.ids(scope_b) == {theirs.id}


def test_sync_keeps_a_users_title_and_archive_flag(fake, store, make_client):
    session = fake.add_session()
    sync, scope, client = make_sync(fake, store, make_client)
    client.submit(session.id, [user_message("server title")])
    sync.sync()
    store.set_title(scope, session.id, "my title")
    store.set_archived(scope, session.id, True)
    sync.sync()
    record = store.get(scope, session.id)
    assert (record.title, record.archived) == ("my title", True)


def test_sync_pages_through_a_long_list(fake, store, make_client):
    ids = {fake.add_session().id for _ in range(7)}
    sync, scope, _ = make_sync(fake, store, make_client, page_size=3)
    result = sync.sync()
    assert result.complete and result.imported == 7 and store.ids(scope) == ids
    assert [r.args["offset"] for r in fake.requests_to("GET", "^/sessions$")] == ["0", "3", "6"]
    assert {r.args["limit"] for r in fake.requests_to("GET", "^/sessions$")} == {"3"}


def test_sync_stops_after_the_safety_limit(fake, store, make_client, monkeypatch):
    monkeypatch.setattr("etcode_desktop.core.sync.MAX_PAGES", 2)
    for _ in range(7):
        fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client, page_size=2)
    result = sync.sync()
    assert result.complete is False and result.imported == 4
    assert not fake.requests_to("GET", "^/session/[0-9a-f]+$")  # nothing is marked missing from an incomplete list


def test_a_session_the_backend_no_longer_has_is_marked_missing(fake, store, make_client):
    keep, gone = fake.add_session(), fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.sync()
    del fake.sessions[gone.id]
    result = sync.sync()
    assert result.marked_missing == 1
    assert store.get(scope, gone.id).is_missing and not store.get(scope, keep.id).is_missing
    assert sync.sync().marked_missing == 0  # not asked about again


def test_a_missing_session_that_comes_back_is_restored(fake, store, make_client):
    session = fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.sync()
    del fake.sessions[session.id]
    sync.sync()
    fake.sessions[session.id] = session
    sync.sync()
    assert store.get(scope, session.id).status == "idle"


def test_a_session_that_is_not_listed_but_still_exists_is_not_marked_missing(fake, store, make_client):
    # the list is a snapshot: a session can slip between pages. The client asks before it believes.
    session = fake.add_session()
    sync, scope, client = make_sync(fake, store, make_client)
    sync.sync()
    original = client.list_sessions
    client.list_sessions = lambda **kw: type(original(**kw))(sessions=(), total=0)  # lists nothing
    result = sync.sync()
    assert result.marked_missing == 0 and result.refreshed == 1
    assert store.get(scope, session.id).status == "idle"


def test_archived_sessions_are_not_checked(fake, store, make_client):
    session = fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.sync()
    store.set_archived(scope, session.id, True)
    del fake.sessions[session.id]
    assert sync.sync().refreshed == 0
    assert not store.get(scope, session.id).is_missing


def test_skipped_sessions_are_not_checked_individually(fake, store, make_client):
    session = fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.sync()
    del fake.sessions[session.id]
    assert sync.sync(skip={session.id}).refreshed == 0


def test_an_empty_backend_marks_everything_local_as_missing_after_checking(fake, store, make_client):
    fake.add_session()
    fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.sync()
    fake.sessions.clear()
    assert sync.sync().marked_missing == 2


# --- errors leave the store alone --------------------------------------------------------------------------------------

def test_an_unreachable_backend_changes_nothing(fake, store, make_client):
    fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.sync()
    before = store.list(scope)
    fake.fail_next("GET", "^/sessions$", status=503, times=10)
    with pytest.raises(ServerError):
        sync.sync()
    assert store.list(scope) == before


def test_a_connection_failure_changes_nothing(fake, store, make_client):
    fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.sync()
    before = store.list(scope)
    dead_client = make_client("http://127.0.0.1:9")
    dead = SessionSync(dead_client, store, scope)
    with pytest.raises(Unreachable):
        dead.sync()
    assert store.list(scope) == before


def test_a_wrong_token_is_reported(fake, store, make_client):
    sync, scope, _ = make_sync(fake, store, make_client, token="wrong")
    with pytest.raises(AuthError):
        sync.sync()
    assert store.list(scope) == []


def test_a_failure_in_the_middle_keeps_what_was_learned_and_marks_nothing(fake, store, make_client):
    a, b = fake.add_session(), fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.sync()
    del fake.sessions[a.id]
    del fake.sessions[b.id]
    fake.sessions[b.id] = b  # b is back, a is gone
    fake.fail_next("GET", "^/session/[0-9a-f]+$", status=503, times=100)  # the double checks fail
    with pytest.raises(ServerError):
        sync.sync()
    assert not store.get(scope, a.id).is_missing  # a 503 is not a 404


# --- a backend without GET /sessions -------------------------------------------------------------------------------------

def test_without_the_list_endpoint_known_sessions_are_refreshed_one_by_one(legacy_fake, store, make_client):
    session = legacy_fake.add_session()
    sync, scope, client = make_sync(legacy_fake, store, make_client)
    sync.attach(session.id)
    session.status = "generating"
    result = sync.sync()
    assert (result.supported, result.complete, result.refreshed) == (False, False, 1)
    assert store.get(scope, session.id).status == "generating"


def test_without_the_list_endpoint_a_session_that_returns_404_is_marked(legacy_fake, store, make_client):
    keep, gone = legacy_fake.add_session(), legacy_fake.add_session()
    sync, scope, _ = make_sync(legacy_fake, store, make_client)
    sync.attach(keep.id)
    sync.attach(gone.id)
    del legacy_fake.sessions[gone.id]
    result = sync.sync()
    assert result.marked_missing == 1 and store.get(scope, gone.id).is_missing


def test_without_the_list_endpoint_the_refresh_is_bounded_and_skips_followed_sessions(legacy_fake, store, make_client):
    sessions = [legacy_fake.add_session() for _ in range(6)]
    sync, scope, _ = make_sync(legacy_fake, store, make_client, max_refresh=3)
    for s in sessions:
        sync.attach(s.id)
    legacy_fake.requests.clear()
    newest = [r.id for r in store.list(scope)]
    sync.sync(skip={newest[0]})
    asked = [r.path.rsplit("/", 1)[1] for r in legacy_fake.requests_to("GET", "^/session/[0-9a-f]+$")]
    assert asked == newest[1:4]  # the three most recent ones, without the skipped one


def test_without_the_list_endpoint_nothing_local_means_no_requests(legacy_fake, store, make_client):
    sync, scope, _ = make_sync(legacy_fake, store, make_client)
    result = sync.sync()
    assert (result.supported, result.refreshed) == (False, 0)


# --- create -------------------------------------------------------------------------------------------------------------------------------

def test_create_returns_with_the_id_already_stored(fake, store, make_client):
    sync, scope, client = make_sync(fake, store, make_client)
    record = sync.create("fake-coder", websearch=True, temperature=0.3)
    assert store.get(scope, record.id) == record
    assert record.model == "fake-coder" and record.status == "idle" and record.title == ""
    assert fake.sessions[record.id].websearch is True and fake.sessions[record.id].options["temperature"] == 0.3


def test_the_id_is_stored_before_any_later_step_can_fail(fake, store, make_client):
    sync, scope, client = make_sync(fake, store, make_client)
    seen = []
    original = store.upsert_from_meta

    def spy(scope_, meta, **kw):
        seen.append(meta.id)
        return original(scope_, meta, **kw)

    store.upsert_from_meta = spy
    record = sync.create("fake-coder")
    assert seen == [record.id]


def test_a_failed_creation_stores_nothing(fake, store, make_client):
    sync, scope, _ = make_sync(fake, store, make_client)
    with pytest.raises(Exception):
        sync.create("no-such-model")
    fake.fail_next("POST", "^/session$", status=503)
    with pytest.raises(ServerError):
        sync.create("fake-coder")
    assert store.list(scope) == []


def test_a_store_failure_after_creation_names_the_session(fake, store, make_client):
    sync, scope, _ = make_sync(fake, store, make_client)
    store.close()
    with pytest.raises(StoreError) as info:
        sync.create("fake-coder")
    created = next(iter(fake.sessions))
    assert created in str(info.value) and "could not be saved locally" in str(info.value)


def test_a_session_that_was_created_but_not_saved_is_found_by_the_next_sync(fake, store, make_client, tmp_path):
    sync, scope, _ = make_sync(fake, store, make_client)
    session = fake.add_session()  # exists on the backend only, as after a crash
    assert store.get(scope, session.id) is None
    sync.sync()
    assert store.get(scope, session.id) is not None


# --- attach --------------------------------------------------------------------------------------------------------------------------------

def test_attach_adds_a_session_by_id(fake, store, make_client):
    session = fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    record = sync.attach(f"  {session.id}\n")
    assert record.id == session.id and store.get(scope, session.id) == record


def test_attach_refuses_unknown_and_foreign_sessions(fake, store, make_client):
    foreign = fake.add_session(owner="tok-b")
    sync, scope, _ = make_sync(fake, store, make_client)
    with pytest.raises(NotFound):
        sync.attach(foreign.id)
    with pytest.raises(NotFound):
        sync.attach("0" * 32)
    with pytest.raises(ValueError):
        sync.attach("   ")
    assert store.list(scope) == []


def test_attach_twice_is_harmless(fake, store, make_client):
    session = fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.attach(session.id)
    sync.attach(session.id)
    assert len(store.list(scope)) == 1
