from __future__ import annotations

import hashlib

from ..catalog import ModelInfo
from ..errors import ProviderError
from .base import OpenAICompatProvider


class ArenaProvider(OpenAICompatProvider):
    """
    Arena's OpenAI-compatible API.
    """

    name = "arena"

    def configured(self) -> bool:
        return bool(self.settings.arena_api_key)

    def endpoint(self) -> str:
        return self.settings.arena_base_url.rstrip("/") + "/chat/completions"

    def headers(self, first_message: str | None = None) -> dict[str, str]:
        if not self.settings.arena_api_key:
            raise ProviderError("not_configured", "ARENA_API_KEY is not configured")
        if first_message:
            session = 'ses_' + hashlib.sha256(first_message.encode()).hexdigest()[0:24]
        else:
            session = 'ses_457891723471973284759180'
        headers = {
            "Authorization": f"Bearer {self.settings.arena_api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "opencode/latest/2.2.5/cli",
            "x-opencode-client": "cli",
            'x-opencode-session': session,
            'x-opencode-session-id': session,
            'x-session-id': session,
            'x-session-affinity': session,
        }
        return headers

    def never_retry(self, status: int) -> bool:
        return status == 403

    def extra_body(self, model: ModelInfo, options: dict) -> dict:
        body: dict = {"stream_options": {"include_usage": True}}
        if options.get("reasoning_effort"):
            body["reasoning_effort"] = options["reasoning_effort"]
        return body
