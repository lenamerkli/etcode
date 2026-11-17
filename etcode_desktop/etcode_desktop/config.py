"""Layered settings.

Each key is resolved on its own, highest priority first:

1. environment (``ETCODE_BACKEND_URL``, ``ETCODE_API_TOKEN``), mostly for development
2. GUI overrides, ``config.json`` in the user config dir (written by this app)
3. values provisioned by the manager, ``/etc/etcode/desktop.json`` (this app only reads it)
4. defaults

So overriding only the URL in the GUI keeps the provisioned token. A bad value in a
layer is logged and skipped, and the next layer is used. A broken file never stops
the app from starting.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .paths import Paths

log = logging.getLogger(__name__)

DEFAULT_BACKEND_URL = "http://10.0.2.2:8765"  # the host, as seen from QEMU user-mode networking
DEFAULT_MAX_PARALLEL_TOOLS = 8
MAX_PARALLEL_TOOLS_LIMIT = 64

KEY_URL = "backend_url"
KEY_TOKEN = "token"
KEY_PARALLEL = "max_parallel_tools"  # deprecated: accepted and validated, but tool calls now run one at a time
KEY_VM_ID = "vm_id"

ENV_URL = "ETCODE_BACKEND_URL"
ENV_TOKEN = "ETCODE_API_TOKEN"
ENV_VM_ID = "ETCODE_VM_ID"

SOURCE_ENV = "env"
SOURCE_GUI = "gui"
SOURCE_PROVISIONED = "provisioned"
SOURCE_DEFAULT = "default"


# --- validation -------------------------------------------------------------------

def normalize_url(value: Any) -> str:
    """Return a clean ``http(s)://host[:port][/prefix]`` URL, or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError("the backend URL must be a string")
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError("the backend URL must start with http:// or https://")
    try:
        host = parts.hostname
        parts.port  # noqa: B018  (raises ValueError for a bad port)
    except ValueError as exc:
        raise ValueError(f"invalid backend URL: {exc}") from exc
    if not host:
        raise ValueError("the backend URL has no host")
    if parts.username or parts.password:
        raise ValueError("put the token in the token field, not in the URL")
    if parts.query or parts.fragment:
        raise ValueError("the backend URL must not have a query or fragment")
    return value.rstrip("/")


def normalize_token(value: Any) -> str | None:
    """Return the token, or ``None`` if it is empty. Raise ``ValueError`` if it can't be a header value."""
    if not isinstance(value, str):
        raise ValueError("the token must be a string")
    value = value.strip()  # a pasted token often carries a trailing newline
    if not value:
        return None
    if any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("the token contains spaces or characters that are not allowed in an HTTP header")
    return value


def normalize_vm_id(value: Any) -> str:
    """Validate a stable local VM/device identity (UUID supplied by the manager)."""
    if not isinstance(value, str):
        raise ValueError("vm_id must be a string")
    value = value.strip()
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("vm_id must be a UUID") from exc


def normalize_parallel(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_parallel_tools must be an integer")
    if not 1 <= value <= MAX_PARALLEL_TOOLS_LIMIT:
        raise ValueError(f"max_parallel_tools must be between 1 and {MAX_PARALLEL_TOOLS_LIMIT}")
    return value


# normalizer returns None for "not set"
_NORMALIZERS: dict[str, Callable[[Any], Any]] = {
    KEY_URL: normalize_url,
    KEY_TOKEN: normalize_token,
    KEY_PARALLEL: normalize_parallel,
    KEY_VM_ID: normalize_vm_id,
}
_DEFAULTS: dict[str, Any] = {
    KEY_URL: DEFAULT_BACKEND_URL,
    KEY_TOKEN: None,
    KEY_PARALLEL: DEFAULT_MAX_PARALLEL_TOOLS,
    KEY_VM_ID: None,
}
_ENV_KEYS = {ENV_URL: KEY_URL, ENV_TOKEN: KEY_TOKEN, ENV_VM_ID: KEY_VM_ID}


def token_fingerprint(token: str) -> str:
    """Stable, non-reversible token identifier for legacy local-store scopes."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


# --- resolved settings ------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    backend_url: str
    token: str = field(default="", repr=False)  # "" means: no token configured
    # Deprecated. Kept so that old config files and the manager still validate; it no longer
    # controls how many calls of one tool round run at once (they always run serially).
    max_parallel_tools: int = DEFAULT_MAX_PARALLEL_TOOLS
    vm_id: str = ""
    # key -> where the value came from (SOURCE_*). For display only.
    sources: Mapping[str, str] = field(default_factory=dict, compare=False, hash=False)

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    @property
    def token_fingerprint(self) -> str:
        return token_fingerprint(self.token)

    def source_of(self, key: str) -> str:
        return self.sources.get(key, SOURCE_DEFAULT)


# --- files --------------------------------------------------------------------------

def _warn_if_group_or_world_readable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("%s is accessible by other users (mode %o) and may contain the token", path, stat.S_IMODE(mode))


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read a settings file. A missing, unreadable or malformed file counts as empty."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("cannot read %s: %s", path, exc)
        return {}
    _warn_if_group_or_world_readable(path)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("ignoring %s: invalid JSON (%s)", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("ignoring %s: expected a JSON object", path)
        return {}
    return data


def _write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    """Write ``path`` with mode 0600 so that readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # mkstemp creates 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _clean_layer(raw: Mapping[str, Any], origin: str) -> dict[str, Any]:
    """Validate the known keys of one layer. Invalid or unset keys are dropped, unknown keys ignored."""
    clean: dict[str, Any] = {}
    for key, normalize in _NORMALIZERS.items():
        if key not in raw:
            continue
        try:
            value = normalize(raw[key])
        except ValueError as exc:
            log.warning("ignoring %s from %s: %s", key, origin, exc)  # never log the value: it may be the token
            continue
        if value is not None:
            clean[key] = value
    return clean


def is_insecure_url(url: str) -> bool:
    """True for an ``http://`` URL that leaves the machine over a network that is not private (the token would be sent
    in clear text). Loopback, private and link-local addresses, ``localhost`` and ``.local`` names are fine."""
    import ipaddress

    parts = urlsplit(url.strip())
    if parts.scheme != "http":
        return False
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # a name on the internet
    return not (address.is_private or address.is_loopback or address.is_link_local)


# --- the store ----------------------------------------------------------------------

class ConfigStore:
    """Loads the layered settings and saves the GUI layer."""

    def __init__(self, paths: Paths, environ: Mapping[str, str] | None = None):
        self.paths = paths
        self._environ = os.environ if environ is None else environ

    def load(self) -> Settings:
        env_raw = {key: self._environ[var] for var, key in _ENV_KEYS.items() if var in self._environ}
        env_layer = _clean_layer(env_raw, "the environment")
        gui_layer = _clean_layer(_read_json_object(self.paths.user_config_file), str(self.paths.user_config_file))
        provisioned_layer = _clean_layer(_read_json_object(self.paths.provisioned_file), str(self.paths.provisioned_file))
        layers = (
            (SOURCE_ENV, env_layer),
            (SOURCE_GUI, gui_layer),
            (SOURCE_PROVISIONED, provisioned_layer),
        )
        values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for key, default in _DEFAULTS.items():
            for source, layer in layers:
                if key in layer:
                    values[key], sources[key] = layer[key], source
                    break
            else:
                values[key], sources[key] = default, SOURCE_DEFAULT
        # VM identity is independent of the bearer token. Prefer the manager's immutable
        # per-VM UUID, otherwise keep a generated UUID in the user's config across restarts.
        if KEY_VM_ID in env_layer:
            vm_id, vm_source = env_layer[KEY_VM_ID], SOURCE_ENV
        elif KEY_VM_ID in provisioned_layer:
            vm_id, vm_source = provisioned_layer[KEY_VM_ID], SOURCE_PROVISIONED
        elif KEY_VM_ID in gui_layer:
            vm_id, vm_source = gui_layer[KEY_VM_ID], SOURCE_GUI
        else:
            vm_id, vm_source = str(uuid.uuid4()), SOURCE_GUI
            try:
                self.save_overrides({KEY_VM_ID: vm_id})
            except OSError as exc:
                log.warning("could not persist local VM identity: %s", exc)
        values[KEY_VM_ID], sources[KEY_VM_ID] = vm_id, vm_source
        if sources.get(KEY_PARALLEL, SOURCE_DEFAULT) != SOURCE_DEFAULT:
            log.info("max_parallel_tools is deprecated and ignored: tool calls now run one at a time")
        return Settings(
            backend_url=values[KEY_URL],
            token=values[KEY_TOKEN] or "",
            max_parallel_tools=values[KEY_PARALLEL],
            vm_id=vm_id,
            sources=sources,
        )

    def provisioned_values(self) -> dict[str, Any]:
        """What the manager provided (only valid values). Used to show what 'reset' goes back to."""
        return _clean_layer(_read_json_object(self.paths.provisioned_file), str(self.paths.provisioned_file))

    def override_values(self) -> dict[str, Any]:
        """What was set in the GUI (only valid values)."""
        return _clean_layer(_read_json_object(self.paths.user_config_file), str(self.paths.user_config_file))

    def env_keys(self) -> set[str]:
        """The settings that an environment variable sets: the GUI cannot change them."""
        return {key for var, key in _ENV_KEYS.items() if self._environ.get(var)}

    def save_overrides(self, updates: Mapping[str, Any]) -> None:
        """Set GUI overrides. A value of ``None`` removes the override for that key.

        Raises ``ValueError`` for an unknown key or a bad value, and then changes nothing.
        Unknown keys that are already in the file are kept.
        """
        cleaned: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in _NORMALIZERS:
                raise ValueError(f"unknown setting: {key}")
            cleaned[key] = None if value is None else _NORMALIZERS[key](value)
        existing = _read_json_object(self.paths.user_config_file)
        for key, value in cleaned.items():
            if value is None:
                existing.pop(key, None)
            else:
                existing[key] = value
        _write_json_atomic(self.paths.user_config_file, existing)

    def reset_to_provisioned(self, keys: tuple[str, ...] = (KEY_URL, KEY_TOKEN)) -> None:
        """Remove the GUI overrides, so the provisioned values (or the defaults) apply again."""
        self.save_overrides({key: None for key in keys})
