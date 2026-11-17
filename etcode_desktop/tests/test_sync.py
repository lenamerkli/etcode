import pytest

pytest.importorskip("flask", reason="the fake backend needs Flask (pip install -e .[dev])")

from etcode_desktop.core.errors import AuthError, NotFound, ServerError
from etcode_desktop.core.models import SessionMeta
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


def test_sync_never_polls_session_list_and_only_refreshes_saved_ids(fake, store, make_client):
    session = fake.add_session()
    sync, scope, client = make_sync(fake, store, make_client)

    # A chat on the backend is not discovered just because this credential can see it.
    result = sync.sync()
    assert result.refreshed == 0
    assert store.list(scope) == []
    assert not fake.requests_to("GET", "^/sessions$")

    # Once explicitly attached, refresh uses only GET /session/<id>.
    sync.attach(session.id)
    session.status = "generating"
    fake.requests.clear()
    result = sync.sync()
    assert result.refreshed == 1
    assert store.get(scope, session.id).status == "generating"
    assert not fake.requests_to("GET", "^/sessions$")
    assert fake.requests_to("GET", f"^/session/{session.id}$")


def test_create_persists_its_id_before_returning(fake, store, make_client):
    sync, scope, _ = make_sync(fake, store, make_client)
    record = sync.create("fake-coder", websearch=True, temperature=0.3)
    assert store.get(scope, record.id) == record
    assert fake.sessions[record.id].websearch is True
    assert fake.sessions[record.id].options["temperature"] == 0.3
    assert not fake.requests_to("GET", "^/sessions$")


def test_attach_adds_a_session_by_id_only_after_backend_authorizes_it(fake, store, make_client):
    mine = fake.add_session(owner="tok-a")
    foreign = fake.add_session(owner="tok-b")
    sync, scope, _ = make_sync(fake, store, make_client)

    assert sync.attach(f" {mine.id}\n").id == mine.id
    with pytest.raises(NotFound):
        sync.attach(foreign.id)
    with pytest.raises(NotFound):
        sync.attach("0" * 32)
    with pytest.raises(ValueError):
        sync.attach("  ")
    assert store.ids(scope) == {mine.id}


def test_404_marks_only_a_previously_saved_session_missing(fake, store, make_client):
    session = fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.attach(session.id)
    del fake.sessions[session.id]

    result = sync.sync()
    assert result.marked_missing == 1
    assert store.get(scope, session.id).is_missing
    assert not fake.requests_to("GET", "^/sessions$")


def test_archived_and_followed_sessions_are_not_refreshed(fake, store, make_client):
    archived, followed = fake.add_session(), fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.attach(archived.id)
    sync.attach(followed.id)
    store.set_archived(scope, archived.id, True)
    fake.requests.clear()

    assert sync.sync(skip={followed.id}).refreshed == 0
    assert not fake.requests_to("GET", "^/sessions$")
    assert not fake.requests_to("GET", "^/session/")


def test_transient_metadata_failure_does_not_mark_a_saved_session_missing(fake, store, make_client):
    session = fake.add_session()
    sync, scope, _ = make_sync(fake, store, make_client)
    sync.attach(session.id)
    before = store.get(scope, session.id)
    fake.fail_next("GET", f"^/session/{session.id}$", status=503)

    with pytest.raises(ServerError):
        sync.sync()
    assert store.get(scope, session.id) == before
    assert not store.get(scope, session.id).is_missing


def test_wrong_token_is_reported_when_refreshing_a_known_session(fake, store, make_client):
    session = fake.add_session(owner="tok-a")
    allowed, scope, _ = make_sync(fake, store, make_client, token="tok-a")
    allowed.attach(session.id)
    denied, _, _ = make_sync(fake, store, make_client, token="wrong")
    # Make the denied client's local scope contain a known ID to refresh.
    denied_scope = denied.scope
    store.upsert_from_meta(denied_scope, SessionMeta(id=session.id, model="fake-coder", status="idle"))
    with pytest.raises(AuthError):
        denied.sync()
    assert not fake.requests_to("GET", "^/sessions$")


def test_store_failure_after_create_reports_the_session_id(fake, store, make_client):
    sync, _, _ = make_sync(fake, store, make_client)
    store.close()
    with pytest.raises(StoreError) as info:
        sync.create("fake-coder")
    created = next(iter(fake.sessions))
    assert created in str(info.value)
    assert "could not be saved locally" in str(info.value)
