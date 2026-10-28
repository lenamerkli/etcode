from conftest import A, B, PNG, FakeUpstream, messages, new_session, say, wait_status
import io


def test_auth_required(client):
    assert client.get("/models").status_code == 401
    assert client.get("/models", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/models", headers={"Authorization": "Bearer tok-a"}).status_code == 200


def test_models_catalog(client):
    models = client.get("/models", headers=A).json
    by = {m["tech_name"]: m for m in models}
    assert by["arena-coding-router"]["provider_tech_name"] == "coding-router-preview"
    assert "openrouter_internal_provider" not in by["arena-coding-router"]
    assert by["deepseek-v4.1-flash-1017"]["openrouter_internal_provider"] == "together"
    assert all(m["available"] for m in models)


def test_session_lifecycle_and_ownership(client):
    sid = new_session(client, websearch=True, temperature=0.5)
    meta = client.get(f"/session/{sid}", headers=A).json
    assert meta["websearch"] and meta["options"] == {"temperature": 0.5} and meta["status"] == "idle"
    assert meta["message_count"] == 1
    for path in (f"/session/{sid}", f"/session/{sid}/messages", f"/session/{sid}/last/1", f"/session/{sid}/stream"):
        assert client.get(path, headers=B).status_code == 404
    assert client.post(f"/session/{sid}/cancel", headers=B).status_code == 404
    r = client.post(f"/session/{sid}/message", json={"role": "user", "content": [{"type": "text", "text": "x"}]}, headers=B)
    assert r.status_code == 404


def test_validation(client):
    assert client.post("/session", json={"model": "nope"}, headers=A).status_code == 400
    assert client.post("/session", json={"model": "arena-coding-router", "temperature": 9}, headers=A).status_code == 400
    assert client.post("/session", data="x", headers=A).status_code == 400
    sid = new_session(client)
    bad = client.post(f"/session/{sid}/message", json={"role": "assistant", "text": {"text": "hi"}}, headers=A)
    assert bad.status_code == 400
    bad = client.post(f"/session/{sid}/message", json={"role": "user", "content": []}, headers=A)
    assert bad.status_code == 400
    bad = client.post(f"/session/{sid}/message", json={"role": "user", "content": [{"type": "image", "hash": "0" * 64}]}, headers=A)
    assert bad.status_code == 404


def test_model_change_rules(client):
    sid = new_session(client)
    r = client.patch(f"/session/{sid}/model", json={"model": "deepseek-v4.1-flash-1017"}, headers=A)
    assert r.status_code == 200 and r.json["model"] == "deepseek-v4.1-flash-1017"
    assert client.patch(f"/session/{sid}/model", json={"model": "x"}, headers=A).status_code == 400


def test_stream_204_when_idle(client):
    sid = new_session(client)
    assert client.get(f"/session/{sid}/stream", headers=A).status_code == 204


def test_image_upload_download_and_scoping(client):
    r = client.post("/media/images", data={"file": (io.BytesIO(PNG), "a.png")}, headers=A, content_type="multipart/form-data")
    assert r.status_code == 201 and r.json["mime_type"] == "image/png"
    h = r.json["hash"]
    again = client.post("/media/images", data={"file": (io.BytesIO(PNG), "b.png")}, headers=A, content_type="multipart/form-data")
    assert again.json["hash"] == h
    got = client.get(f"/media/images/{h}", headers=A)
    assert got.status_code == 200 and got.data == PNG and got.mimetype == "image/png"
    assert client.get(f"/media/images/{h}", headers=B).status_code == 404
    assert client.get("/media/images/../../etc/passwd", headers=A).status_code == 404
    bad = client.post("/media/images", data={"file": (io.BytesIO(b"hello"), "a.png")}, headers=A, content_type="multipart/form-data")
    assert bad.status_code == 415
    assert client.post("/media/images", headers=A).status_code == 400


def test_last_n_uses_absolute_indexes(client, worker, upstream):
    sid = new_session(client)
    upstream.script = [FakeUpstream.text_reply("hello")]
    say(client, sid, "hi")
    wait_status(client, sid, "idle")
    r = client.get(f"/session/{sid}/last/2", headers=A).json
    assert [m["index"] for m in r] == [1, 2]
    assert r[1]["message"]["text"]["text"] == "hello"
    assert client.get(f"/session/{sid}/last/abc", headers=A).status_code == 400
    assert len(messages(client, sid)) == 3
