"""Application settings and tool discovery.

The manager keeps its state under the ``etcode-manager`` directory:

* ``out/etcode-base-v1-amd64.qcow2`` — immutable template image
* ``vms/<vm-dir>/``                  — one directory per VM (disk, seed, state)
* ``config.json``                    — the tracked defaults (``base_image`` only)
* ``config.local.json``              — git-ignored user overrides (backend URL/token, scaling, ...)

``config.json`` is under version control and must stay free of secrets, so it holds nothing but
``base_image``. Everything else lives in ``config.local.json``, which is git-ignored and written with
mode 0600. A key in ``config.local.json`` wins over the same key in ``config.json``.

``backend_url`` and ``backend_token`` in ``config.local.json`` are the defaults for the etcode backend that the
desktop app inside a VM talks to. They are copied into each new VM (``vm.json`` and the cloud-init seed).

``ssh_public_key`` authorizes the host's SSH key in new VMs. ``scaling`` selects the default screen filter
for new manager windows (``area`` or ``lanczos``). These values can be changed from the manager's Settings
dialog, which writes them back to ``config.local.json``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# etcode-manager/ (parent of the etcode_manager package)
MANAGER_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_BASE_IMAGE = MANAGER_ROOT / "out" / "etcode-base-v1-amd64.qcow2"
LEGACY_BASE_IMAGE = MANAGER_ROOT / "build" / "debian-13-generic-amd64.qcow2"
DEFAULT_VMS_DIR = MANAGER_ROOT / "vms"
CONFIG_FILE = MANAGER_ROOT / "config.json"            # tracked: base_image (and legacy keys) only
LOCAL_CONFIG_FILE = MANAGER_ROOT / "config.local.json"  # git-ignored: everything else (holds the token)

# The host as seen from a guest behind QEMU's user-mode networking (``-netdev user``).
DEFAULT_BACKEND_URL = "http://10.0.2.2:8765"

# Screen resampling defaults ("area" matches ``ScalingFilter.AREA``). Kept as strings so that this module
# (and config-only code such as the seed writer) does not need to import Qt.
DEFAULT_SCALING = "area"
SCALING_CHOICES = ("area", "lanczos")
_SCALING_ALIASES = {
    "area": "area",
    "smooth": "area",
    "lanczos": "lanczos",
    "sharper": "lanczos",
}

_CONFIG_KEYS = (
    "base_image", "vms_dir", "ssh_public_key", "backend_url", "backend_token", "scaling", "scaling_filter",
)


def normalize_backend_url(value: object) -> str:
    """Return a clean ``http(s)://host[:port][/prefix]`` URL (the default for an empty value), or raise ``ValueError``.

    The rules are those of the desktop app (``etcode_desktop.config.normalize_url``): a URL the desktop would
    refuse must not be written into a VM.
    """
    if not isinstance(value, str):
        raise ValueError("The backend URL must be text.")
    value = value.strip()
    if not value:
        return DEFAULT_BACKEND_URL
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError("The backend URL must start with http:// or https://.")
    try:
        host = parts.hostname
        parts.port  # noqa: B018 (raises ValueError for a bad port)
    except ValueError as exc:
        raise ValueError(f"Invalid backend URL: {exc}") from exc
    if not host:
        raise ValueError("The backend URL has no host.")
    if parts.username or parts.password:
        raise ValueError("Put the token in the token field, not in the URL.")
    if parts.query or parts.fragment:
        raise ValueError("The backend URL must not have a query or a fragment.")
    return value.rstrip("/")


def normalize_backend_token(value: object) -> str:
    """Return the token ("" if there is none), or raise ``ValueError`` if it cannot be an HTTP header value."""
    if not isinstance(value, str):
        raise ValueError("The token must be text.")
    value = value.strip()  # a pasted token often carries a trailing newline
    if any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("The token contains spaces or characters that are not allowed in an HTTP header.")
    return value


_SSH_PUBLIC_KEY_RE = re.compile(r"(?:ssh|ecdsa|sk)-[\w@.+-]+[ \t]+[A-Za-z0-9+/]+={0,2}(?:[ \t]+[^\r\n]*)?")


def normalize_ssh_public_key(value: object) -> str:
    """Accept a blank value or a single OpenSSH .pub line, never a private key."""
    if isinstance(value, str):
        value = value.strip()
        if not value or _SSH_PUBLIC_KEY_RE.fullmatch(value):
            return value
    raise ValueError("Paste one OpenSSH public key (.pub), not a private key.")


def normalize_scaling(value: object) -> str:
    """Return a canonical scaling name (``area`` or ``lanczos``), or raise ``ValueError``.

    The user-facing names from the selector ("Smooth", "Sharper") are accepted too, so a hand-written
    ``config.local.json`` can use whichever name is clearer.
    """
    if not isinstance(value, str):
        raise ValueError("The scaling algorithm must be text.")
    key = value.strip().lower()
    if not key:
        return DEFAULT_SCALING
    try:
        return _SCALING_ALIASES[key]
    except KeyError as exc:
        raise ValueError(
            f"Unknown scaling algorithm {value!r}; use 'area' (Smooth) or 'lanczos' (Sharper)."
        ) from exc


def _find_tool(*candidates: str) -> Path | None:
    """Locate an executable in PATH, with a few well-known extra locations."""
    for name in candidates:
        found = shutil.which(name)
        if found:
            return Path(found)
    if sys.platform == "win32":
        for name in candidates:
            for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),):
                p = Path(base) / "qemu" / name
                if p.exists():
                    return p
    return None


@dataclass
class Settings:
    """Resolved application settings."""

    base_image: Path = DEFAULT_BASE_IMAGE
    vms_dir: Path = DEFAULT_VMS_DIR
    qemu_system: Path | None = None
    qemu_img: Path | None = None
    ssh_public_key: str = ""
    backend_url: str = DEFAULT_BACKEND_URL     # default for new VMs: where the desktop app in the VM finds the backend
    backend_token: str = field(default="", repr=False)  # default for new VMs. A secret: never print or log it
    scaling: str = DEFAULT_SCALING             # default screen filter for the manager window ("area" / "lanczos")
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Also normalises an enum or the user-facing alias when Settings is built in code.
        self.scaling = normalize_scaling(self.scaling)

    @property
    def scaling_filter(self):
        """The default as a :class:`~etcode_manager.scaling.ScalingFilter` (imported lazily; config stays Qt-free)."""
        from .scaling import ScalingFilter
        return ScalingFilter(self.scaling)

    @scaling_filter.setter
    def scaling_filter(self, value) -> None:
        self.scaling = normalize_scaling(getattr(value, "value", value))

    def require_tools(self) -> None:
        missing = [
            name
            for name, tool in (
                ("qemu-system-x86_64", self.qemu_system),
                ("qemu-img", self.qemu_img),
            )
            if tool is None
        ]
        if missing:
            raise RuntimeError(
                "Missing required tools: "
                + ", ".join(missing)
                + ". Install QEMU (Fedora: dnf install qemu-kvm / "
                "Debian: apt-get install qemu-system-x86 qemu-utils / "
                "Windows: https://qemu.weilnetz.de/w64/)."
            )


def _read_config(path: Path, verb: str = "read") -> dict:
    """Return the JSON object stored in ``path`` (``{}`` when the file does not exist), or raise ``RuntimeError``."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot {verb} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Cannot {verb} {path}: the top level is not a JSON object.")
    return data


def _write_config(path: Path, data: dict) -> None:
    """Write ``data`` as JSON to ``path`` atomically with mode 0600 (the file can hold the token)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=4)
            stream.write("\n")
        try:
            os.chmod(tmp, 0o600)  # a leftover temp file keeps its old mode otherwise
        except OSError:
            pass  # not every file system has modes (Windows)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_settings() -> Settings:
    """Load settings from ``config.json`` and the git-ignored ``config.local.json`` (if present) and detect tools.

    Both files hold JSON objects and are merged key by key; a key in ``config.local.json`` wins over the same
    key in ``config.json``, so the tracked file can keep carrying legacy values for old checkouts.
    """
    tracked = _read_config(CONFIG_FILE)
    local = _read_config(LOCAL_CONFIG_FILE)
    cfg = {**tracked, **local}

    def source(key: str) -> Path:
        """The file a key actually comes from (for error messages)."""
        return LOCAL_CONFIG_FILE if key in local else CONFIG_FILE

    base_image = Path(cfg.get("base_image", DEFAULT_BASE_IMAGE)).expanduser()
    if not base_image.exists() and base_image == DEFAULT_BASE_IMAGE and LEGACY_BASE_IMAGE.exists():
        base_image = LEGACY_BASE_IMAGE

    try:
        backend_url = normalize_backend_url(cfg.get("backend_url", ""))
    except ValueError as exc:
        raise RuntimeError(f"Invalid backend_url in {source('backend_url')}: {exc}") from exc
    try:
        backend_token = normalize_backend_token(cfg.get("backend_token", ""))
    except ValueError as exc:  # the message never contains the token
        raise RuntimeError(f"Invalid backend_token in {source('backend_token')}: {exc}") from exc
    try:
        ssh_public_key = normalize_ssh_public_key(cfg.get("ssh_public_key", ""))
    except ValueError as exc:
        raise RuntimeError(f"Invalid ssh_public_key in {source('ssh_public_key')}: {exc}") from exc
    try:
        # ``scaling_filter`` is the historical/descriptive alias; ``scaling`` wins when both exist.
        scaling = normalize_scaling(cfg.get("scaling", cfg.get("scaling_filter", DEFAULT_SCALING)))
    except ValueError as exc:
        raise RuntimeError(f"Invalid scaling setting in {source('scaling')}: {exc}") from exc

    settings = Settings(
        base_image=base_image.resolve(),
        vms_dir=Path(cfg.get("vms_dir", DEFAULT_VMS_DIR)).expanduser().resolve(),
        qemu_system=_find_tool("qemu-system-x86_64", "qemu-system-x86_64.exe"),
        qemu_img=_find_tool("qemu-img", "qemu-img.exe"),
        ssh_public_key=ssh_public_key,
        backend_url=backend_url,
        backend_token=backend_token,
        scaling=scaling,
        extra={k: v for k, v in cfg.items() if k not in _CONFIG_KEYS},
    )
    return settings


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Persist the user-editable defaults to the git-ignored ``config.local.json`` and return the file path.

    The backend URL/token, SSH public key and scaling default are written; other keys already in the file
    (``vms_dir``, custom keys, ...) are kept. Any key except ``base_image`` that the tracked ``config.json``
    still holds is moved here too (values already present win), so the tracked file converges to the one key
    that belongs in version control and no secret stays tracked. The file can contain a token, so it is
    written atomically with mode 0600, just like ``vm.json`` and the seed ISO.
    """
    path = LOCAL_CONFIG_FILE if path is None else path
    data = _read_config(path, verb="update")

    data["backend_url"] = normalize_backend_url(settings.backend_url)
    data["backend_token"] = normalize_backend_token(settings.backend_token)
    ssh_public_key = normalize_ssh_public_key(settings.ssh_public_key)
    if ssh_public_key:
        data["ssh_public_key"] = ssh_public_key
    else:
        data.pop("ssh_public_key", None)
    data["scaling"] = normalize_scaling(settings.scaling)
    data.pop("scaling_filter", None)  # the alias is read for compatibility; write only the canonical key

    # Spin everything but ``base_image`` out of the tracked ``config.json`` (migrating the values this file
    # does not hold yet) so that nothing user- or machine-specific — and no secret — stays in version control.
    tracked = {} if CONFIG_FILE == path else _read_config(CONFIG_FILE, verb="update")
    pruned: dict = {}
    for key, value in tracked.items():
        if key == "base_image":
            pruned[key] = value
        elif key not in ("scaling_filter", "ssh_public_key"):  # the alias and edited SSH key are handled above
            data.setdefault(key, value)

    _write_config(path, data)
    if pruned != tracked:  # only touch the tracked file when it still carries spun-out keys
        _write_config(CONFIG_FILE, pruned)
    return path


def detect_accel() -> str:
    """Pick the best available QEMU accelerator for this host."""
    if sys.platform.startswith("linux") and Path("/dev/kvm").exists():
        return "kvm"
    if sys.platform == "win32":
        return "whpx"
    return "tcg"