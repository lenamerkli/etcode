import sqlite3
import stat
import threading
import time

import pytest

from etcode_desktop.core.models import Cost, SessionMeta
from etcode_desktop.core.store import (
    SCHEMA_VERSION, STATUS_MISSING, Scope, SessionRecord, SessionStore, StoreError,
)

URL = "http://10.0.2.2:8765"
SCOPE = Scope.for_token(URL, "tok-alice")
OTHER_TOKEN = Scope.for_token(URL, "tok-bob")
OTHER_URL = Scope.for_token("https://server.example", "tok-alice")


def meta(session_id="s1", *, title=None, status="idle", model="m", cost="0", created=1000.0, updated=None):
    return SessionMeta(id=session_id, model=model, status=status, title=title, created_at=created,
                       updated_at=created if updated is None else updated,
                       cost=Cost(total=float(cost), total_exact=cost))


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "data" / "store.sqlite3")
    yield s
    s.close()


# --- scope -----------------------------------------------------------------------------------

def test_scope_uses_a_fingerprint_not_the_token():
    assert SCOPE.token_fp != "tok-alice" and len(SCOPE.token_fp) == 16
    assert Scope.for_token(URL + "/", "tok-alice") == SCOPE  # a trailing slash is the same backend
    assert OTHER_TOKEN != SCOPE != OTHER_URL


def test_sessions_are_isolated_by_token_and_by_url(store):
    store.upsert_from_meta(SCOPE, meta("s1"))
    store.upsert_from_meta(OTHER_TOKEN, meta("s2"))
    store.upsert_from_meta(OTHER_URL, meta("s3"))
    assert [r.id for r in store.list(SCOPE)] == ["s1"]
    assert [r.id for r in store.list(OTHER_TOKEN)] == ["s2"]
    assert [r.id for r in store.list(OTHER_URL)] == ["s3"]
    assert store.get(SCOPE, "s2") is None and store.get(OTHER_TOKEN, "s1") is None


def test_the_same_id_can_exist_in_two_scopes(store):
    store.upsert_from_meta(SCOPE, meta("same", title="a"))
    store.upsert_from_meta(OTHER_TOKEN, meta("same", title="b"))
    assert store.get(SCOPE, "same").title == "a" and store.get(OTHER_TOKEN, "same").title == "b"
    store.set_archived(SCOPE, "same", True)
    assert not store.get(OTHER_TOKEN, "same").archived


def test_every_write_is_scoped(store):
    store.upsert_from_meta(SCOPE, meta("s1"))
    store.upsert_from_meta(OTHER_TOKEN, meta("s1"))
    store.set_title(OTHER_TOKEN, "s1", "mine")
    store.set_archived(OTHER_TOKEN, "s1", True)
    store.mark_opened(OTHER_TOKEN, "s1", 5.0)
    store.advance_last_seen(OTHER_TOKEN, "s1", 9)
    assert store.mark_missing(OTHER_TOKEN, "s1")
    untouched = store.get(SCOPE, "s1")
    assert (untouched.title, untouched.archived, untouched.last_opened, untouched.last_seen_index, untouched.status) == ("", False, None, None, "idle")


# --- adding, reading, ordering ---------------------------------------------------------------------

def test_add_and_read_back_all_fields(store):
    record, created = store.upsert_from_meta(SCOPE, meta("s1", title="Fix bug", status="generating", model="fake",
                                                          cost="0.0123", created=1000.0, updated=2000.0))
    assert created is True
    assert record == SessionRecord(id="s1", backend_url=URL, token_fp=SCOPE.token_fp, title="Fix bug", title_custom=False,
                                   model="fake", status="generating", cost_total="0.0123", created_at=1000.0,
                                   updated_at=2000.0, last_opened=None, last_seen_index=None, archived=False)
    assert store.get(SCOPE, "s1") == record
    assert store.get(SCOPE, "nope") is None


def test_list_is_most_recent_activity_first(store):
    store.upsert_from_meta(SCOPE, meta("old", updated=100.0, created=1.0))
    store.upsert_from_meta(SCOPE, meta("new", updated=300.0, created=2.0))
    store.upsert_from_meta(SCOPE, meta("mid", updated=200.0, created=3.0))
    assert [r.id for r in store.list(SCOPE)] == ["new", "mid", "old"]


def test_ties_are_broken_by_creation_time_then_id(store):
    store.upsert_from_meta(SCOPE, meta("a", updated=100.0, created=1.0))
    store.upsert_from_meta(SCOPE, meta("b", updated=100.0, created=2.0))
    store.upsert_from_meta(SCOPE, meta("c", updated=100.0, created=2.0))
    assert [r.id for r in store.list(SCOPE)] == ["c", "b", "a"]


def test_a_second_upsert_updates_and_reports_not_new(store):
    store.upsert_from_meta(SCOPE, meta("s1", status="idle", model="m1", cost="0", updated=100.0))
    record, created = store.upsert_from_meta(SCOPE, meta("s1", status="generating", model="m2", cost="0.5", updated=200.0))
    assert created is False
    assert (record.status, record.model, record.cost_total, record.updated_at) == ("generating", "m2", "0.5", 200.0)
    assert record.created_at == 1000.0  # unchanged
    assert len(store.list(SCOPE)) == 1


def test_missing_timestamps_fall_back_to_now(store):
    record, _ = store.upsert_from_meta(SCOPE, SessionMeta(id="s1", model="m", status="idle"))
    assert record.created_at > 1e9 and record.updated_at == record.created_at


# --- titles -----------------------------------------------------------------------------------------------

def test_server_title_is_used(store):
    store.upsert_from_meta(SCOPE, meta("s1", title="From the server"))
    assert store.get(SCOPE, "s1").title == "From the server"


def test_a_local_title_fills_a_gap_but_the_server_title_replaces_it(store):
    store.upsert_from_meta(SCOPE, meta("s1"), title="local guess")
    assert store.get(SCOPE, "s1").title == "local guess"
    store.upsert_from_meta(SCOPE, meta("s1"))  # no server title yet: the local one stays
    assert store.get(SCOPE, "s1").title == "local guess"
    store.upsert_from_meta(SCOPE, meta("s1", title="server title"))
    assert store.get(SCOPE, "s1").title == "server title"


def test_a_local_title_does_not_replace_an_existing_title(store):
    store.upsert_from_meta(SCOPE, meta("s1", title="server title"))
    store.upsert_from_meta(SCOPE, meta("s1"), title="local guess")
    assert store.get(SCOPE, "s1").title == "server title"


def test_a_renamed_session_keeps_the_users_title(store):
    store.upsert_from_meta(SCOPE, meta("s1", title="server title"))
    store.set_title(SCOPE, "s1", "  My   own\ttitle ")
    record = store.get(SCOPE, "s1")
    assert (record.title, record.title_custom) == ("My own title", True)
    store.upsert_from_meta(SCOPE, meta("s1", title="a different server title"), title="local")
    store.merge_listing(SCOPE, [meta("s1", title="yet another")])
    assert store.get(SCOPE, "s1").title == "My own title"


def test_an_empty_rename_goes_back_to_automatic_titles(store):
    store.upsert_from_meta(SCOPE, meta("s1", title="server title"))
    store.set_title(SCOPE, "s1", "mine")
    store.set_title(SCOPE, "s1", "   ")
    assert store.get(SCOPE, "s1").title_custom is False
    store.merge_listing(SCOPE, [meta("s1", title="server title 2")])
    assert store.get(SCOPE, "s1").title == "server title 2"


def test_set_title_if_unset(store):
    store.upsert_from_meta(SCOPE, meta("s1"))
    store.set_title_if_unset(SCOPE, "s1", "  first\nmessage ")
    assert store.get(SCOPE, "s1").title == "first message"
    store.set_title_if_unset(SCOPE, "s1", "second")
    assert store.get(SCOPE, "s1").title == "first message"
    store.set_title_if_unset(SCOPE, "s1", "   ")


# --- merging a listing --------------------------------------------------------------------------------------------

def test_merge_imports_unknown_and_updates_known(store):
    store.upsert_from_meta(SCOPE, meta("known", status="idle", updated=100.0))
    result = store.merge_listing(SCOPE, [meta("known", status="generating", updated=200.0), meta("fresh", title="Imported")])
    assert result.imported == ("fresh",) and result.updated == ("known",)
    assert store.get(SCOPE, "known").status == "generating" and store.get(SCOPE, "fresh").title == "Imported"


def test_merge_is_atomic(store):
    bad = SessionMeta(id="b", model="m", status="idle")
    object.__setattr__(bad, "cost", None)  # makes the write fail halfway through
    with pytest.raises(AttributeError):
        store.merge_listing(SCOPE, [meta("a"), bad])
    assert store.list(SCOPE) == []


def test_merge_keeps_local_flags(store):
    store.upsert_from_meta(SCOPE, meta("s1"))
    store.set_archived(SCOPE, "s1", True)
    store.mark_opened(SCOPE, "s1", 42.0)
    store.advance_last_seen(SCOPE, "s1", 7)
    store.merge_listing(SCOPE, [meta("s1", status="generating")])
    record = store.get(SCOPE, "s1")
    assert (record.archived, record.last_opened, record.last_seen_index, record.status) == (True, 42.0, 7, "generating")


def test_a_missing_session_comes_back_when_the_backend_lists_it_again(store):
    store.upsert_from_meta(SCOPE, meta("s1"))
    assert store.mark_missing(SCOPE, "s1")
    assert store.get(SCOPE, "s1").is_missing and store.get(SCOPE, "s1").status == STATUS_MISSING
    store.merge_listing(SCOPE, [meta("s1", status="idle")])
    assert store.get(SCOPE, "s1").status == "idle"


def test_mark_missing_reports_whether_anything_changed(store):
    assert store.mark_missing(SCOPE, "unknown") is False
    store.upsert_from_meta(SCOPE, meta("s1"))
    assert store.mark_missing(SCOPE, "s1") is True
    assert store.mark_missing(SCOPE, "s1") is False


def test_unlisted_skips_archived_and_missing_sessions(store):
    for name in ("listed", "gone", "archived", "already-missing"):
        store.upsert_from_meta(SCOPE, meta(name))
    store.set_archived(SCOPE, "archived", True)
    store.mark_missing(SCOPE, "already-missing")
    store.upsert_from_meta(OTHER_TOKEN, meta("other-scope"))
    assert store.unlisted(SCOPE, {"listed"}) == ["gone"]


# --- archive, opened, last seen ----------------------------------------------------------------------------------------

def test_archiving(store):
    store.upsert_from_meta(SCOPE, meta("s1"))
    store.upsert_from_meta(SCOPE, meta("s2"))
    store.set_archived(SCOPE, "s1", True)
    assert [r.id for r in store.list(SCOPE)] == ["s2"]
    assert {r.id for r in store.list(SCOPE, include_archived=True)} == {"s1", "s2"}
    assert store.ids(SCOPE) == {"s1", "s2"} and store.ids(SCOPE, include_archived=False) == {"s2"}
    store.set_archived(SCOPE, "s1", False)
    assert {r.id for r in store.list(SCOPE)} == {"s1", "s2"}


def test_mark_opened(store):
    store.upsert_from_meta(SCOPE, meta("s1"))
    store.mark_opened(SCOPE, "s1")
    assert store.get(SCOPE, "s1").last_opened > 1e9
    store.mark_opened(SCOPE, "s1", 123.0)
    assert store.get(SCOPE, "s1").last_opened == 123.0


def test_last_seen_index_only_moves_forward(store):
    store.upsert_from_meta(SCOPE, meta("s1"))
    store.advance_last_seen(SCOPE, "s1", 3)
    store.advance_last_seen(SCOPE, "s1", 1)
    assert store.get(SCOPE, "s1").last_seen_index == 3
    store.advance_last_seen(SCOPE, "s1", 0)
    store.advance_last_seen(SCOPE, "s1", 8)
    assert store.get(SCOPE, "s1").last_seen_index == 8
    store.upsert_from_meta(SCOPE, meta("s2"))
    store.advance_last_seen(SCOPE, "s2", 0)
    assert store.get(SCOPE, "s2").last_seen_index == 0


def test_writes_to_unknown_sessions_do_nothing(store):
    store.set_title(SCOPE, "nope", "x")
    store.set_archived(SCOPE, "nope", True)
    store.mark_opened(SCOPE, "nope")
    store.advance_last_seen(SCOPE, "nope", 1)
    assert store.list(SCOPE, include_archived=True) == []


# --- key/value ------------------------------------------------------------------------------------------------------------------

def test_kv_round_trip(store):
    assert store.kv_get("x") is None and store.kv_get("x", 5) == 5
    for value in ("text", 3, 1.5, True, None, ["a", 1], {"k": {"n": [1, 2]}}):
        store.kv_set("x", value)
        assert store.kv_get("x", "default") == value
    store.kv_delete("x")
    assert store.kv_get("x") is None
    store.kv_delete("x")  # no error


def test_kv_ignores_a_value_that_is_not_json(store):
    with store._write() as connection:
        connection.execute("INSERT INTO kv(key,value) VALUES ('bad','{not json')")
    assert store.kv_get("bad", "fallback") == "fallback"


# --- persistence and files -------------------------------------------------------------------------------------------------------

def test_data_survives_reopening(tmp_path):
    path = tmp_path / "store.sqlite3"
    first = SessionStore(path)
    first.upsert_from_meta(SCOPE, meta("s1", title="kept"))
    first.set_title(SCOPE, "s1", "custom")
    first.set_archived(SCOPE, "s1", True)
    first.kv_set("model", "fake")
    first.close()
    second = SessionStore(path)
    record = second.get(SCOPE, "s1")
    assert (record.title, record.title_custom, record.archived) == ("custom", True, True)
    assert second.kv_get("model") == "fake"
    second.close()


def test_the_database_is_private(tmp_path):
    path = tmp_path / "private" / "store.sqlite3"
    store = SessionStore(path)
    store.upsert_from_meta(SCOPE, meta("s1"))
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        if candidate.exists():
            assert stat.S_IMODE(candidate.stat().st_mode) == 0o600, candidate
    store.close()


def test_the_token_is_never_written_to_disk(tmp_path):
    path = tmp_path / "store.sqlite3"
    store = SessionStore(path)
    store.upsert_from_meta(SCOPE, meta("s1"))
    store.kv_set("k", "v")
    store.close()
    content = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert b"tok-alice" not in content and SCOPE.token_fp.encode() in content


def test_the_schema_version_is_recorded_and_reopening_does_not_migrate_twice(tmp_path):
    path = tmp_path / "store.sqlite3"
    SessionStore(path).close()
    SessionStore(path).close()
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {"sessions", "kv"}
    connection.close()


def test_a_newer_database_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "store.sqlite3"
    SessionStore(path).close()
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    connection.close()
    before = path.read_bytes()
    with pytest.raises(StoreError, match="newer version"):
        SessionStore(path)
    assert path.read_bytes() == before and not list(tmp_path.glob("*.corrupt-*"))


def test_a_corrupt_database_is_set_aside_and_replaced(tmp_path, caplog):
    path = tmp_path / "store.sqlite3"
    path.write_bytes(b"this is not a sqlite database at all" * 100)
    store = SessionStore(path)
    assert store.list(SCOPE) == []
    store.upsert_from_meta(SCOPE, meta("s1"))
    assert store.get(SCOPE, "s1") is not None
    aside = list(tmp_path.glob("store.sqlite3.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_bytes().startswith(b"this is not")
    assert "unreadable" in caplog.text
    store.close()


def test_a_corrupt_database_with_stale_wal_files(tmp_path):
    path = tmp_path / "store.sqlite3"
    path.write_bytes(b"garbage" * 500)
    (tmp_path / "store.sqlite3-wal").write_bytes(b"stale wal")
    (tmp_path / "store.sqlite3-shm").write_bytes(b"stale shm")
    store = SessionStore(path)
    store.upsert_from_meta(SCOPE, meta("s1"))
    assert [r.id for r in store.list(SCOPE)] == ["s1"]
    store.close()


def test_using_a_closed_store_is_an_error(tmp_path):
    store = SessionStore(tmp_path / "store.sqlite3")
    store.close()
    with pytest.raises(StoreError):
        store.list(SCOPE)


def test_creates_missing_parent_directories(tmp_path):
    store = SessionStore(tmp_path / "a" / "b" / "store.sqlite3")
    assert store.list(SCOPE) == []
    store.close()


# --- threads ------------------------------------------------------------------------------------------------------------------------------

def test_concurrent_writers_from_many_threads(store):
    errors = []

    def work(worker):
        try:
            for i in range(25):
                store.upsert_from_meta(SCOPE, meta(f"w{worker}-{i}", updated=float(i)))
                store.advance_last_seen(SCOPE, f"w{worker}-{i}", i)
                store.list(SCOPE)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(store.list(SCOPE)) == 150


def test_a_connection_per_thread(store):
    seen = {}

    def work():
        seen["thread"] = store._connect()

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert seen["thread"] is not store._connect()


def test_a_failed_write_rolls_back_and_leaves_the_store_usable(store):
    with pytest.raises(RuntimeError):
        with store._write() as connection:
            connection.execute("INSERT INTO kv(key,value) VALUES ('half','1')")
            raise RuntimeError("boom")
    assert store.kv_get("half") is None
    store.kv_set("after", 1)
    assert store.kv_get("after") == 1


def test_two_stores_on_the_same_file_see_each_others_writes(tmp_path):
    path = tmp_path / "store.sqlite3"
    one, two = SessionStore(path), SessionStore(path)
    one.upsert_from_meta(SCOPE, meta("s1"))
    assert two.get(SCOPE, "s1") is not None
    two.set_archived(SCOPE, "s1", True)
    assert one.get(SCOPE, "s1").archived
    one.close()
    two.close()


# --- the tool journal ---------------------------------------------------------------------------------------------------

from etcode_desktop.core.store import JOURNAL_DONE, JOURNAL_POSTED, JOURNAL_RUNNING, MIGRATIONS  # noqa: E402

RESULT = [{"type": "text", "text": "<returncode>0</returncode>"}]


def test_a_call_goes_from_running_to_done_to_posted(store):
    assert store.journal_get(SCOPE, "s1", "c1") is None
    assert store.journal_start(SCOPE, "s1", "c1", "bash", {"command": "ls"}) is True
    entry = store.journal_get(SCOPE, "s1", "c1")
    assert (entry.name, entry.arguments, entry.state, entry.result, entry.finished_at) == ("bash", {"command": "ls"}, JOURNAL_RUNNING, None, None)
    assert entry.started_at > 1e9
    store.journal_done(SCOPE, "s1", "c1", RESULT)
    entry = store.journal_get(SCOPE, "s1", "c1")
    assert (entry.state, entry.result) == (JOURNAL_DONE, RESULT) and entry.finished_at >= entry.started_at
    store.journal_posted(SCOPE, "s1", "c1")
    assert store.journal_get(SCOPE, "s1", "c1").state == JOURNAL_POSTED


def test_starting_twice_changes_nothing_and_says_so(store):
    assert store.journal_start(SCOPE, "s1", "c1", "bash", {"command": "first"})
    store.journal_done(SCOPE, "s1", "c1", RESULT)
    assert store.journal_start(SCOPE, "s1", "c1", "bash", {"command": "second"}) is False
    entry = store.journal_get(SCOPE, "s1", "c1")
    assert entry.arguments == {"command": "first"} and entry.state == JOURNAL_DONE and entry.result == RESULT


def test_states_only_move_forward(store):
    store.journal_start(SCOPE, "s1", "c1", "bash", {})
    store.journal_posted(SCOPE, "s1", "c1")  # not done yet: no effect
    assert store.journal_get(SCOPE, "s1", "c1").state == JOURNAL_RUNNING
    store.journal_done(SCOPE, "s1", "c1", RESULT)
    store.journal_posted(SCOPE, "s1", "c1")
    store.journal_done(SCOPE, "s1", "c1", [{"type": "text", "text": "different"}])  # already posted: no effect
    entry = store.journal_get(SCOPE, "s1", "c1")
    assert entry.state == JOURNAL_POSTED and entry.result == RESULT


def test_the_journal_belongs_to_a_backend_and_a_session_not_to_a_token(store):
    store.journal_start(SCOPE, "s1", "c1", "bash", {})
    store.journal_done(SCOPE, "s1", "c1", RESULT)
    # the same call after the token was changed: it is known, so it will not be run again
    assert store.journal_get(OTHER_TOKEN, "s1", "c1").state == JOURNAL_DONE
    assert store.journal_start(OTHER_TOKEN, "s1", "c1", "bash", {}) is False
    # another backend or another session is another story
    assert store.journal_get(OTHER_URL, "s1", "c1") is None and store.journal_get(SCOPE, "s2", "c1") is None
    assert store.journal_start(OTHER_URL, "s1", "c1", "bash", {}) is True


def test_journal_entries_of_a_session_in_start_order(store):
    for call_id in ("c3", "c1", "c2"):
        store.journal_start(SCOPE, "s1", call_id, "bash", {"n": call_id})
        time.sleep(0.002)
    store.journal_start(SCOPE, "s2", "other", "bash", {})
    assert [e.call_id for e in store.journal_entries(SCOPE, "s1")] == ["c3", "c1", "c2"]


def test_pruning_forgets_only_old_posted_calls(store):
    for call_id in ("old-posted", "old-done", "old-running", "new-posted"):
        store.journal_start(SCOPE, "s1", call_id, "bash", {})
    for call_id in ("old-posted", "old-done", "new-posted"):
        store.journal_done(SCOPE, "s1", call_id, RESULT)
    for call_id in ("old-posted", "new-posted"):
        store.journal_posted(SCOPE, "s1", call_id)
    future = time.time() + 30 * 86400
    with store._write() as connection:  # age the old ones
        connection.execute("UPDATE tool_journal SET started_at=1, finished_at=1 WHERE call_id LIKE 'old-%'")
    assert store.journal_prune(keep_seconds=86400) == 1
    assert {e.call_id for e in store.journal_entries(SCOPE, "s1")} == {"old-done", "old-running", "new-posted"}
    assert store.journal_prune(keep_seconds=86400, now=future) == 1  # the new one gets old, too, later
    assert {e.call_id for e in store.journal_entries(SCOPE, "s1")} == {"old-done", "old-running"}


def test_a_version_1_database_is_upgraded_and_keeps_its_data(tmp_path):
    path = tmp_path / "store.sqlite3"
    connection = sqlite3.connect(path)
    for statement in [s for s in MIGRATIONS[0].split(";") if s.strip()]:
        connection.execute(statement)
    connection.execute("PRAGMA user_version=1")
    connection.execute("INSERT INTO sessions(id,backend_url,token_fp,title,created_at,updated_at) VALUES ('s1','u','t','kept',1,1)")
    connection.commit()
    connection.close()
    store = SessionStore(path)
    assert store.kv_get("anything") is None
    assert store.journal_start(Scope("u", "t"), "s1", "c1", "bash", {}) is True
    assert store.get(Scope("u", "t"), "s1").title == "kept"
    store.close()
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    check.close()


def test_upgrading_to_version_3_merges_the_journal_of_different_tokens(tmp_path):
    path = tmp_path / "store.sqlite3"
    connection = sqlite3.connect(path)
    for migration in MIGRATIONS[:2]:
        for statement in [s for s in migration.split(";") if s.strip()]:
            connection.execute(statement)
    connection.execute("PRAGMA user_version=2")
    rows = [  # url, token, session, call, state, result, started
        ("u", "tokA", "s1", "same", "running", None, 1.0), ("u", "tokB", "s1", "same", "posted", '[{"type":"text","text":"B"}]', 2.0),
        ("u", "tokA", "s1", "only-a", "done", '[{"type":"text","text":"A"}]', 3.0), ("u", "tokA", "s2", "same", "running", None, 4.0),
        ("v", "tokA", "s1", "same", "done", '[{"type":"text","text":"V"}]', 5.0)]
    for url, token, session, call, state, result, started in rows:
        connection.execute("INSERT INTO tool_journal VALUES (?,?,?,?,'bash','{}',?,?,?,NULL)", (url, token, session, call, state, result, started))
    connection.commit()
    connection.close()
    store = SessionStore(path)
    scope_u, scope_v = Scope("u", "x"), Scope("v", "x")
    assert store.journal_get(scope_u, "s1", "same").state == JOURNAL_POSTED  # the furthest along wins
    assert store.journal_get(scope_u, "s1", "same").result == [{"type": "text", "text": "B"}]
    assert store.journal_get(scope_u, "s1", "only-a").state == JOURNAL_DONE
    assert store.journal_get(scope_u, "s2", "same").state == JOURNAL_RUNNING
    assert store.journal_get(scope_v, "s1", "same").result == [{"type": "text", "text": "V"}]
    assert len(store.journal_entries(scope_u, "s1")) == 2
    store.close()
