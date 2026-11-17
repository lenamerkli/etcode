import json
import logging
import stat

import pytest

from etcode_desktop.config import (
    DEFAULT_BACKEND_URL, DEFAULT_MAX_PARALLEL_TOOLS, ConfigStore, KEY_PARALLEL, KEY_TOKEN, KEY_URL,
    SOURCE_DEFAULT, SOURCE_ENV, SOURCE_GUI, SOURCE_PROVISIONED, Settings,
    normalize_parallel, normalize_token, normalize_url, token_fingerprint,
)


def write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    path.chmod(mode)


def store(paths, environ=None):
    return ConfigStore(paths, environ={} if environ is None else environ)


# --- layering -------------------------------------------------------------------------

def test_defaults_when_nothing_is_configured(paths):
    s = store(paths).load()
    assert s.backend_url == DEFAULT_BACKEND_URL == "http://10.0.2.2:8765"
    assert s.token == "" and not s.has_token
    assert s.max_parallel_tools == DEFAULT_MAX_PARALLEL_TOOLS == 8
    assert {s.source_of(k) for k in (KEY_URL, KEY_TOKEN, KEY_PARALLEL)} == {SOURCE_DEFAULT}


def test_provisioned_values_are_used(paths):
    write(paths.provisioned_file, {"backend_url": "https://srv.example/", "token": "prov-token"})
    s = store(paths).load()
    assert (s.backend_url, s.token) == ("https://srv.example", "prov-token")
    assert s.source_of(KEY_URL) == s.source_of(KEY_TOKEN) == SOURCE_PROVISIONED


def test_gui_overrides_provisioned_and_env_overrides_gui(paths):
    write(paths.provisioned_file, {"backend_url": "http://prov:1", "token": "prov-token"})
    write(paths.user_config_file, {"backend_url": "http://gui:2", "token": "gui-token"})
    s = store(paths).load()
    assert (s.backend_url, s.token) == ("http://gui:2", "gui-token")
    assert s.source_of(KEY_URL) == s.source_of(KEY_TOKEN) == SOURCE_GUI

    s = store(paths, {"ETCODE_BACKEND_URL": "http://env:3", "ETCODE_API_TOKEN": "env-token"}).load()
    assert (s.backend_url, s.token) == ("http://env:3", "env-token")
    assert s.source_of(KEY_URL) == s.source_of(KEY_TOKEN) == SOURCE_ENV


def test_keys_are_resolved_separately(paths):
    write(paths.provisioned_file, {"backend_url": "http://prov:1", "token": "prov-token"})
    write(paths.user_config_file, {"backend_url": "http://gui:2"})  # no token override
    s = store(paths).load()
    assert s.backend_url == "http://gui:2" and s.source_of(KEY_URL) == SOURCE_GUI
    assert s.token == "prov-token" and s.source_of(KEY_TOKEN) == SOURCE_PROVISIONED


def test_empty_values_count_as_not_set(paths):
    write(paths.provisioned_file, {"token": "prov-token"})
    write(paths.user_config_file, {"token": "   "})
    s = store(paths, {"ETCODE_API_TOKEN": ""}).load()
    assert s.token == "prov-token" and s.source_of(KEY_TOKEN) == SOURCE_PROVISIONED


def test_invalid_value_falls_through_to_next_layer(paths, caplog):
    write(paths.provisioned_file, {"backend_url": "http://prov:1"})
    write(paths.user_config_file, {"backend_url": "ftp://nope", "max_parallel_tools": 0})
    with caplog.at_level(logging.WARNING):
        s = store(paths).load()
    assert s.backend_url == "http://prov:1" and s.source_of(KEY_URL) == SOURCE_PROVISIONED
    assert s.max_parallel_tools == 8 and s.source_of(KEY_PARALLEL) == SOURCE_DEFAULT
    assert "backend_url" in caplog.text and "max_parallel_tools" in caplog.text


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"text"', "", "\xff\xfe"])
def test_broken_files_are_ignored(paths, content, caplog):
    write(paths.user_config_file, content)
    with caplog.at_level(logging.WARNING):
        s = store(paths).load()
    assert s.backend_url == DEFAULT_BACKEND_URL


def test_unknown_keys_are_ignored(paths):
    write(paths.user_config_file, {"something_else": 1, "backend_url": "http://x:1"})
    assert store(paths).load().backend_url == "http://x:1"


def test_warns_when_a_file_with_secrets_is_world_readable(paths, caplog):
    write(paths.provisioned_file, {"token": "abcd1234"}, mode=0o644)
    with caplog.at_level(logging.WARNING):
        store(paths).load()
    assert "accessible by other users" in caplog.text
    assert "abcd1234" not in caplog.text


def test_invalid_token_is_not_logged(paths, caplog):
    write(paths.user_config_file, {"token": "bad token with spaces"})
    with caplog.at_level(logging.WARNING):
        s = store(paths).load()
    assert not s.has_token
    assert "bad token with spaces" not in caplog.text


# --- saving ---------------------------------------------------------------------------

def test_vm_identity_is_stable_and_independent_of_token(paths):
    first = store(paths).load()
    assert first.vm_id
    store(paths).save_overrides({KEY_TOKEN: "rotated-token"})
    second = store(paths).load()
    assert second.vm_id == first.vm_id


def test_manager_vm_identity_overrides_a_local_fallback(paths):
    vm_id = "12345678-1234-5678-1234-567812345678"
    write(paths.provisioned_file, {"vm_id": vm_id})
    write(paths.user_config_file, {"vm_id": "87654321-4321-8765-4321-876543218765"})
    assert store(paths).load().vm_id == vm_id


def test_save_overrides_round_trip_and_mode(paths):
    st = store(paths)
    st.save_overrides({KEY_URL: "http://10.0.2.2:9000/", KEY_TOKEN: " new-token\n"})
    s = st.load()
    assert s.backend_url == "http://10.0.2.2:9000" and s.token == "new-token"
    assert stat.S_IMODE(paths.user_config_file.stat().st_mode) == 0o600
    saved = json.loads(paths.user_config_file.read_text())
    assert saved == {"backend_url": "http://10.0.2.2:9000", "token": "new-token", "vm_id": s.vm_id}
    assert [p.name for p in paths.config_dir.iterdir()] == ["config.json"]  # no temp files left


def test_save_overrides_creates_the_directory(env):
    from etcode_desktop.paths import Paths
    paths = Paths.discover()  # ensure_dirs not called
    store(paths).save_overrides({KEY_URL: "http://a:1"})
    assert stat.S_IMODE(paths.config_dir.stat().st_mode) == 0o700
    assert paths.user_config_file.is_file()


def test_save_overrides_keeps_other_keys(paths):
    write(paths.user_config_file, {"backend_url": "http://a:1", "future_key": [1, 2]})
    store(paths).save_overrides({KEY_TOKEN: "tok-1234"})
    assert json.loads(paths.user_config_file.read_text()) == {"backend_url": "http://a:1", "future_key": [1, 2], "token": "tok-1234"}


def test_save_overrides_with_none_removes_the_key(paths):
    write(paths.provisioned_file, {"token": "prov-token"})
    st = store(paths)
    st.save_overrides({KEY_TOKEN: "gui-token"})
    assert st.load().token == "gui-token"
    st.save_overrides({KEY_TOKEN: None})
    assert st.load().token == "prov-token"


def test_save_overrides_rejects_bad_input_and_changes_nothing(paths):
    write(paths.user_config_file, {"backend_url": "http://a:1"})
    before = paths.user_config_file.read_text()
    st = store(paths)
    with pytest.raises(ValueError):
        st.save_overrides({KEY_URL: "http://ok:1", KEY_TOKEN: "has space"})
    with pytest.raises(ValueError):
        st.save_overrides({"nonsense": 1})
    assert paths.user_config_file.read_text() == before


def test_reset_to_provisioned(paths):
    write(paths.provisioned_file, {"backend_url": "http://prov:1", "token": "prov-token"})
    st = store(paths)
    st.save_overrides({KEY_URL: "http://gui:2", KEY_TOKEN: "gui-token", KEY_PARALLEL: 3})
    st.reset_to_provisioned()
    s = st.load()
    assert (s.backend_url, s.token) == ("http://prov:1", "prov-token")
    assert s.max_parallel_tools == 3  # only url and token are reset by default


def test_save_over_a_corrupt_file_replaces_it(paths):
    write(paths.user_config_file, "{broken")
    store(paths).save_overrides({KEY_URL: "http://a:1"})
    assert store(paths).load().backend_url == "http://a:1"


# --- validators -----------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("http://10.0.2.2:8765", "http://10.0.2.2:8765"),
    ("  https://host.example/  ", "https://host.example"),
    ("https://host.example/etcode/", "https://host.example/etcode"),
    ("http://[::1]:8765", "http://[::1]:8765"),
])
def test_normalize_url_accepts(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "10.0.2.2:8765", "ftp://host", "http://", "http://host:notaport", "http://host:99999",
    "http://user:pw@host", "http://host/?a=b", "http://host/#frag", None, 5,
])
def test_normalize_url_rejects(raw):
    with pytest.raises(ValueError):
        normalize_url(raw)


def test_normalize_token():
    assert normalize_token("  abc123\n") == "abc123"
    assert normalize_token("") is None and normalize_token(" \n") is None
    for bad in ("a b", "tab\there", "ünïcode", "a\x00b", 5, None):
        with pytest.raises(ValueError):
            normalize_token(bad)


def test_normalize_parallel():
    assert normalize_parallel(1) == 1 and normalize_parallel(64) == 64
    for bad in (0, 65, -1, True, 2.5, "8"):
        with pytest.raises(ValueError):
            normalize_parallel(bad)


# --- secrets --------------------------------------------------------------------------

def test_settings_repr_does_not_contain_the_token():
    s = Settings(backend_url="http://x:1", token="super-secret-token")
    assert "super-secret-token" not in repr(s) and "super-secret-token" not in str(s)


def test_token_fingerprint():
    assert token_fingerprint("a") == token_fingerprint("a") != token_fingerprint("b")
    assert len(token_fingerprint("a")) == 16
    assert all(c in "0123456789abcdef" for c in token_fingerprint("a"))
    assert Settings(backend_url="http://x:1", token="a").token_fingerprint == token_fingerprint("a")


def test_settings_equality_ignores_sources():
    a = Settings(backend_url="http://x:1", token="t", sources={KEY_URL: SOURCE_GUI})
    b = Settings(backend_url="http://x:1", token="t")
    assert a == b


# --- what the connection dialog needs --------------------------------------------------------------------------------------

from etcode_desktop.config import is_insecure_url  # noqa: E402


@pytest.mark.parametrize("url, insecure", [
    ("https://example.com", False), ("https://203.0.113.5:8765", False), ("http://10.0.2.2:8765", False), ("http://192.168.1.20", False),
    ("http://172.16.0.9", False), ("http://127.0.0.1:8765", False), ("http://localhost:8765", False), ("http://[::1]:8765", False),
    ("http://169.254.1.1", False), ("http://backend.local:8765", False), ("http://my.localhost", False),
    ("http://example.com", True), ("http://93.184.216.34:8765", True), ("http://8.8.8.8", True), ("http://sub.example.org:80/etcode", True),
])
def test_http_is_only_a_concern_beyond_the_private_network(url, insecure):
    assert is_insecure_url(url) is insecure


def test_the_layers_can_be_read_separately(paths):
    write(paths.provisioned_file, {"backend_url": "http://prov:1", "token": "prov-token", "max_parallel_tools": 99})  # 99 is invalid
    write(paths.user_config_file, {"token": "gui-token"})
    st = store(paths, {"ETCODE_BACKEND_URL": "http://env:3"})
    assert st.provisioned_values() == {"backend_url": "http://prov:1", "token": "prov-token"}
    assert st.override_values() == {"token": "gui-token"}
    assert st.env_keys() == {KEY_URL}
    assert store(paths, {"ETCODE_API_TOKEN": ""}).env_keys() == set()  # an empty variable does not count
    assert store(paths).provisioned_values() != {} and store(paths).override_values() == {"token": "gui-token"}
