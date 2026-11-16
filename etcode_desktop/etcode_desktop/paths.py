"""Where the app keeps its files (XDG base directories).

========================  ==========================================  =====================
Purpose                   Location                                    Override
========================  ==========================================  =====================
GUI settings              ``$XDG_CONFIG_HOME/etcode_desktop``         ``XDG_CONFIG_HOME``
Session store, caches     ``$XDG_DATA_HOME/etcode_desktop``           ``XDG_DATA_HOME``
Logs                      ``$XDG_STATE_HOME/etcode_desktop``          ``XDG_STATE_HOME``
Single-instance lock      ``$XDG_RUNTIME_DIR`` (else the state dir)   ``XDG_RUNTIME_DIR``
Values from the manager   ``/etc/etcode/desktop.json`` (read only)    ``ETCODE_PROVISIONED_FILE``
========================  ==========================================  =====================
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "etcode_desktop"
DEFAULT_PROVISIONED_FILE = Path("/etc/etcode/desktop.json")
ENV_PROVISIONED_FILE = "ETCODE_PROVISIONED_FILE"


def _xdg_dir(environ: Mapping[str, str], variable: str, fallback: Path) -> Path:
    # The XDG spec says relative values must be ignored.
    value = environ.get(variable, "")
    if value and os.path.isabs(value):
        return Path(value)
    return fallback


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    data_dir: Path
    state_dir: Path
    runtime_dir: Path
    provisioned_file: Path

    @property
    def user_config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def store_file(self) -> Path:
        return self.data_dir / "store.sqlite3"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "desktop.log"

    @property
    def lock_file(self) -> Path:
        return self.runtime_dir / "etcode_desktop.lock"

    @classmethod
    def discover(cls, environ: Mapping[str, str] | None = None) -> Paths:
        environ = os.environ if environ is None else environ
        home = Path(environ.get("HOME") or Path.home())
        state_dir = _xdg_dir(environ, "XDG_STATE_HOME", home / ".local" / "state") / APP_DIR_NAME
        provisioned = environ.get(ENV_PROVISIONED_FILE)
        return cls(
            config_dir=_xdg_dir(environ, "XDG_CONFIG_HOME", home / ".config") / APP_DIR_NAME,
            data_dir=_xdg_dir(environ, "XDG_DATA_HOME", home / ".local" / "share") / APP_DIR_NAME,
            state_dir=state_dir,
            runtime_dir=_xdg_dir(environ, "XDG_RUNTIME_DIR", state_dir),
            provisioned_file=Path(provisioned) if provisioned else DEFAULT_PROVISIONED_FILE,
        )

    def ensure_dirs(self) -> None:
        """Create the private directories (mode 0700). An existing runtime dir is left as it is."""
        for directory in (self.config_dir, self.data_dir, self.state_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
