import stat
from pathlib import Path

from etcode_desktop.paths import DEFAULT_PROVISIONED_FILE, Paths


def test_xdg_variables_are_used():
    paths = Paths.discover({"HOME": "/h", "XDG_CONFIG_HOME": "/c", "XDG_DATA_HOME": "/d",
                            "XDG_STATE_HOME": "/s", "XDG_RUNTIME_DIR": "/r"})
    assert paths.config_dir == Path("/c/etcode_desktop")
    assert paths.data_dir == Path("/d/etcode_desktop")
    assert paths.state_dir == Path("/s/etcode_desktop")
    assert paths.runtime_dir == Path("/r")
    assert paths.user_config_file == Path("/c/etcode_desktop/config.json")
    assert paths.store_file == Path("/d/etcode_desktop/store.sqlite3")
    assert paths.log_file == Path("/s/etcode_desktop/desktop.log")
    assert paths.lock_file == Path("/r/etcode_desktop.lock")


def test_defaults_below_home():
    paths = Paths.discover({"HOME": "/home/agent"})
    assert paths.config_dir == Path("/home/agent/.config/etcode_desktop")
    assert paths.data_dir == Path("/home/agent/.local/share/etcode_desktop")
    assert paths.state_dir == Path("/home/agent/.local/state/etcode_desktop")
    assert paths.runtime_dir == paths.state_dir  # no XDG_RUNTIME_DIR
    assert paths.provisioned_file == DEFAULT_PROVISIONED_FILE == Path("/etc/etcode/desktop.json")


def test_relative_xdg_values_are_ignored():
    paths = Paths.discover({"HOME": "/h", "XDG_CONFIG_HOME": "relative/dir"})
    assert paths.config_dir == Path("/h/.config/etcode_desktop")


def test_provisioned_file_can_be_overridden():
    paths = Paths.discover({"HOME": "/h", "ETCODE_PROVISIONED_FILE": "/tmp/x.json"})
    assert paths.provisioned_file == Path("/tmp/x.json")


def test_ensure_dirs_creates_private_directories(env):
    paths = Paths.discover()
    paths.ensure_dirs()
    for directory in (paths.config_dir, paths.data_dir, paths.state_dir):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    paths.ensure_dirs()  # idempotent
