"""Configuration loaded from environment variables and an optional ``.env`` file.

Environment variables take precedence over values in ``.env``.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # .../etcode_backend
DEFAULT_PORT = 8765


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else default


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value not in (None, "") else default


def parse_tokens(raw: str) -> dict[str, str]:
    """Parse ``ETCODE_API_TOKENS`` into a mapping token -> owner id.

    Entries are separated by commas (or newlines). Each entry is either a bare
    ``token`` (owner id = SHA-256 of the token) or ``name:token`` (owner id =
    ``name``). Named tokens let you rotate a token without losing access to the
    sessions created with it.
    """
    tokens: dict[str, str] = {}
    for entry in raw.replace("\n", ",").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            name, token = entry.split(":", 1)
            name, token = name.strip(), token.strip()
            owner = f"name:{name}"
        else:
            token = entry
            owner = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        if not token:
            raise ValueError("ETCODE_API_TOKENS contains an empty token")
        tokens[token] = owner
    return tokens


@dataclass
class Settings:
    api_tokens: dict[str, str] = field(default_factory=dict)
    data_dir: Path = PACKAGE_ROOT / "data"
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    arena_api_key: str | None = None
    arena_base_url: str = "https://api.arena.ai/v1"
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_referer: str | None = None
    openrouter_title: str | None = "etcode"
    search_provider: str = "brave"
    brave_api_key: str | None = None
    max_upload_bytes: int = 20 * 1024 * 1024
    max_request_bytes: int = 30 * 1024 * 1024
    max_text_chars: int = 2_000_000
    worker_concurrency: int = 8
    worker_poll_interval: float = 0.25
    stale_generation_seconds: float = 90.0
    event_retention_seconds: float = 3600.0
    provider_connect_timeout: float = 30.0
    provider_read_timeout: float = 300.0
    search_timeout: float = 20.0
    stream_poll_interval: float = 0.1
    stream_keepalive_seconds: float = 15.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "backend.sqlite3"

    @property
    def image_dir(self) -> Path:
        return self.data_dir / "images"

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = None) -> "Settings":
        env_path = Path(env_file) if env_file else Path(os.environ.get("ETCODE_ENV_FILE", PACKAGE_ROOT / ".env"))
        if env_path.is_file():
            load_dotenv(env_path, override=False)
        data_dir = Path(os.environ.get("ETCODE_DATA_DIR") or PACKAGE_ROOT / "data")
        return cls(
            api_tokens=parse_tokens(os.environ.get("ETCODE_API_TOKENS", "")),
            data_dir=data_dir,
            host=os.environ.get("ETCODE_HOST", "0.0.0.0"),
            port=_int("ETCODE_PORT", DEFAULT_PORT),
            arena_api_key=os.environ.get("ARENA_API_KEY") or None,
            arena_base_url=os.environ.get("ARENA_BASE_URL", "https://api.arena.ai/v1"),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
            openrouter_base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            openrouter_referer=os.environ.get("OPENROUTER_HTTP_REFERER") or None,
            openrouter_title=os.environ.get("OPENROUTER_TITLE", "etcode") or None,
            search_provider=os.environ.get("ETCODE_SEARCH_PROVIDER", "brave"),
            brave_api_key=os.environ.get("BRAVE_API_KEY") or None,
            max_upload_bytes=_int("ETCODE_MAX_UPLOAD_BYTES", 20 * 1024 * 1024),
            max_request_bytes=_int("ETCODE_MAX_REQUEST_BYTES", 30 * 1024 * 1024),
            worker_concurrency=_int("ETCODE_WORKER_CONCURRENCY", 8),
            stale_generation_seconds=_float("ETCODE_STALE_GENERATION_SECONDS", 90.0),
            event_retention_seconds=_float("ETCODE_EVENT_RETENTION_SECONDS", 3600.0),
            provider_read_timeout=_float("ETCODE_PROVIDER_READ_TIMEOUT", 300.0),
        )
