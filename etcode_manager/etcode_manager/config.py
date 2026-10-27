"""Application settings and tool discovery.

The manager keeps its state under the ``etcode-manager`` directory:

* ``out/etcode-base-v1-amd64.qcow2`` — immutable template image
* ``vms/<vm-dir>/``                  — one directory per VM (disk, seed, state)
* ``config.json``                    — optional user overrides
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# etcode-manager/ (parent of the etcode_manager package)
MANAGER_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_BASE_IMAGE = MANAGER_ROOT / "out" / "etcode-base-v1-amd64.qcow2"
LEGACY_BASE_IMAGE = MANAGER_ROOT / "build" / "debian-13-generic-amd64.qcow2"
DEFAULT_VMS_DIR = MANAGER_ROOT / "vms"
CONFIG_FILE = MANAGER_ROOT / "config.json"


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

    settings = Settings(
        base_image=base_image.resolve(),
        vms_dir=Path(cfg.get("vms_dir", DEFAULT_VMS_DIR)).expanduser().resolve(),
        qemu_system=_find_tool("qemu-system-x86_64", "qemu-system-x86_64.exe"),
        qemu_img=_find_tool("qemu-img", "qemu-img.exe"),
        ssh_public_key=str(cfg.get("ssh_public_key", "")).strip(),
        extra={k: v for k, v in cfg.items() if k not in ("base_image", "vms_dir", "ssh_public_key")},
    )
    return settings


def detect_accel() -> str:
    """Pick the best available QEMU accelerator for this host."""
    if sys.platform.startswith("linux") and Path("/dev/kvm").exists():
        return "kvm"
    if sys.platform == "win32":
        return "whpx"
    return "tcg"