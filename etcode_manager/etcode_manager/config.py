"""Application settings and tool discovery.

The manager keeps its state under the ``etcode-manager`` directory:

* ``out/etcode-base-v1-amd64.qcow2`` — immutable template image
* ``vms/<vm-dir>/``                  — one directory per VM (disk, seed, state)
* ``config.json``                    — optional user overrides

``backend_url`` and ``backend_token`` in ``config.json`` are the defaults for the etcode backend that the
desktop app inside a VM talks to. They are copied into each new VM (``vm.json`` and the cloud-init seed).
"""

from __future__ import annotations

import json
import os
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
CONFIG_FILE = MANAGER_ROOT / "config.json"

# The host as seen from a guest behind QEMU's user-mode networking (``-netdev user``).
DEFAULT_BACKEND_URL = "http://10.0.2.2:8765"

_CONFIG_KEYS = ("base_image", "vms_dir", "ssh_public_key", "backend_url", "backend_token")


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
    extra: dict = field(default_factory=dict)

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


def load_settings() -> Settings:
    """Load settings from ``config.json`` (if present) and detect tools."""
    cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read {CONFIG_FILE}: {exc}") from exc

    base_image = Path(cfg.get("base_image", DEFAULT_BASE_IMAGE)).expanduser()
    if not base_image.exists() and base_image == DEFAULT_BASE_IMAGE and LEGACY_BASE_IMAGE.exists():
        base_image = LEGACY_BASE_IMAGE

    try:
        backend_url = normalize_backend_url(cfg.get("backend_url", ""))
        backend_token = normalize_backend_token(cfg.get("backend_token", ""))
    except ValueError as exc:  # the message never contains the token
        raise RuntimeError(f"Invalid backend setting in {CONFIG_FILE}: {exc}") from exc

    settings = Settings(
        base_image=base_image.resolve(),
        vms_dir=Path(cfg.get("vms_dir", DEFAULT_VMS_DIR)).expanduser().resolve(),
        qemu_system=_find_tool("qemu-system-x86_64", "qemu-system-x86_64.exe"),
        qemu_img=_find_tool("qemu-img", "qemu-img.exe"),
        ssh_public_key=str(cfg.get("ssh_public_key", "")).strip(),
        backend_url=backend_url,
        backend_token=backend_token,
        extra={k: v for k, v in cfg.items() if k not in _CONFIG_KEYS},
    )
    return settings


def detect_accel() -> str:
    """Pick the best available QEMU accelerator for this host."""
    if sys.platform.startswith("linux") and Path("/dev/kvm").exists():
        return "kvm"
    if sys.platform == "win32":
        return "whpx"
    return "tcg"