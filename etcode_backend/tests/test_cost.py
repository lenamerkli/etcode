import sqlite3
import time
from decimal import Decimal

from conftest import A, FakeUpstream, messages, new_session, say, wait_for, wait_status
from etcode_backend import catalog
from etcode_backend.config import Settings
from etcode_backend.db import SCHEMA, connect, initialize
from etcode_backend.providers import get_provider


def cost(client, sid):
    return client.get(f"/session/{sid}", headers=A).json["cost"]


def test_new_session_has_zero_cost(client):
    sid = new_session(client)
    assert cost(client, sid) == {"currency": "USD", "total": 0.0, "total_exact": "0", "unpriced_generations": 0}


def test_costs_accumulate_across_generations_and_tool_rounds(client, worker, upstream):
    upstream.script = [
        FakeUpstream.tool_reply([("c1", "bash", {"command": "ls"})], usage={"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.1}),
        FakeUpstream.text_reply("done", usage={"prompt_tokens": 20, "completion_tokens": 3, "cost": 0.2}),
        FakeUpstream.text_reply("again", usage={"cost": "0.0000001"}),
    ]
    sid = new_session(client, model="deepseek-v4.1-flash-1017")
    say(client, sid, "run")
    wait_status(client, sid, "awaiting_tools")
    assert cost(client, sid)["total_exact"] == "0.1"
    client.post(f"/session/{sid}/message", json={"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "ok"}]}, headers=A)
    wait_status(client, sid, "idle")
    say(client, sid, "more")
    wait_for(lambda: cost(client, sid)["total_exact"] == "0.3000001")  # exact decimal, no float drift
    c = cost(client, sid)
    assert c["total"] == 0.3000001 and c["unpriced_generations"] == 0


def test_usage_event_and_generation_record(client, worker, upstream, settings):
    upstream.script = [FakeUpstream.text_reply("hi", usage={"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.25})]
    sid = new_session(client, model="deepseek-v4.1-flash-1017")
    gid = say(client, sid, "hi")["generation_id"]
    wait_status(client, sid, "idle")
    with connect(settings) as con:
        gen = con.execute("SELECT usage,cost FROM generations WHERE id=?", (gid,)).fetchone()
        assert gen["cost"] == "0.25" and '"prompt_tokens":5' in gen["usage"]
        ev = con.execute("SELECT payload FROM events WHERE generation_id=? AND kind='usage'", (gid,)).fetchone()
    import json
    payload = json.loads(ev["payload"])
    assert payload["cost"] == "0.25" and payload["session_cost_total"] == "0.25" and payload["source"] == "provider"


def test_missing_cost_is_unpriced_not_free(client, worker, upstream):
    upstream.script = [FakeUpstream.text_reply("no usage"), FakeUpstream.text_reply("tokens only", usage={"prompt_tokens": 3, "completion_tokens": 1})]
    sid = new_session(client)
    say(client, sid, "a")
    wait_status(client, sid, "idle")
    say(client, sid, "b")
    wait_for(lambda: cost(client, sid)["unpriced_generations"] == 2)
    assert cost(client, sid)["total_exact"] == "0"


def test_failed_request_without_output_is_not_billed(client, worker, upstream):
    upstream.script = [FakeUpstream.error(403)]
    sid = new_session(client)
    say(client, sid, "x")
    wait_status(client, sid, "failed")
    assert cost(client, sid) == {"currency": "USD", "total": 0.0, "total_exact": "0", "unpriced_generations": 0}


def test_cancelled_generation_with_output_counts_as_unpriced(client, worker, upstream):
    def slow(handler, body):
        FakeUpstream.sse(handler, [{"choices": [{"delta": {"content": "x"}}]} for _ in range(60)], delay=0.05)
    upstream.script = [slow]
    sid = new_session(client)
    gid = say(client, sid, "go")["generation_id"]
    wait_for(lambda: upstream.requests)
    def received_text():
        from etcode_backend.db import connect
        with connect(client.application.config["ETCODE_SETTINGS"]) as con:
            return con.execute("SELECT 1 FROM events WHERE kind='text_delta' AND generation_id=?", (gid,)).fetchone()
    wait_for(received_text)
    client.post(f"/session/{sid}/cancel", headers=A)
    wait_status(client, sid, "idle")
    assert messages(client, sid)[-1]["message"]["status"] == "cancelled"
    assert cost(client, sid)["unpriced_generations"] == 1


def test_catalog_price_fallback(settings):
    model = catalog.ModelInfo(name="m", tech_name="m", context_length=1, supports_images=False, provider="arena",
                              provider_tech_name="m", price_input_per_mtok="3", price_output_per_mtok="15")
    p = get_provider("arena", settings)
    assert p.extract_cost({"prompt_tokens": 1_000_000, "completion_tokens": 100_000}, model) == Decimal("4.5")
    assert p.extract_cost({"cost": 0.01, "prompt_tokens": 1_000_000, "completion_tokens": 0}, model) == Decimal("0.01")
    assert p.extract_cost({"prompt_tokens": 1}, catalog.get_model("arena-coding-router")) is None
    for bad in ({"cost": -1}, {"cost": "nan"}, {"cost": True}, {"cost": None}):
        assert p.extract_cost(bad, catalog.get_model("arena-coding-router")) is None


def test_migrates_existing_database(tmp_path):
    settings = Settings(data_dir=tmp_path)
    old_schema = SCHEMA.replace(",\n cost_total TEXT NOT NULL DEFAULT '0', unpriced_generations INTEGER NOT NULL DEFAULT 0", "") \
                       .replace(",\n usage TEXT, cost TEXT, cost_recorded INTEGER NOT NULL DEFAULT 0", "")
    assert "cost_total" not in old_schema and "cost_recorded" not in old_schema
    con = sqlite3.connect(settings.db_path)
    con.executescript(old_schema)
    con.execute("INSERT INTO sessions VALUES ('s','o','arena-coding-router',0,'{}','idle',0,0)")
    con.commit(); con.close()
    initialize(settings)
    initialize(settings)  # idempotent
    with connect(settings) as con:
        row = con.execute("SELECT cost_total,unpriced_generations FROM sessions WHERE id='s'").fetchone()
        assert (row["cost_total"], row["unpriced_generations"]) == ("0", 0)
        assert "cost_recorded" in {r["name"] for r in con.execute("PRAGMA table_info(generations)")}
