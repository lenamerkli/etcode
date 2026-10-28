from __future__ import annotations

import json
import os

from ..catalog import ModelInfo
from ..errors import ProviderError
from .base import OpenAICompatProvider


class ArenaProvider(OpenAICompatProvider):
    """Arena's OpenAI-compatible API.

    Arena restricts access to specific first-party clients and returns HTTP 403
    otherwise. 403 is never retried. Any additional headers that your Arena
    agreement permits can be supplied via ``ARENA_EXTRA_HEADERS`` (JSON object);
    none are sent by default.
    """

    name = "arena"

    def configured(self) -> bool:
        return bool(self.settings.arena_api_key)

    def endpoint(self) -> str:
        return self.settings.arena_base_url.rstrip("/") + "/chat/completions"

    def headers(self) -> dict[str, str]:
        if not self.settings.arena_api_key:
            raise ProviderError("not_configured", "ARENA_API_KEY is not configured")
        headers = {"Authorization": f"Bearer {self.settings.arena_api_key}", "Content-Type": "application/json",
                   "Accept": "text/event-stream"}
        extra = os.environ.get("ARENA_EXTRA_HEADERS")
        if extra:
            parsed = json.loads(extra)
            headers.update({str(k): str(v) for k, v in parsed.items()})
        return headers

    def never_retry(self, status: int) -> bool:
        return status == 403

    def extra_body(self, model: ModelInfo, options: dict) -> dict:
        body: dict = {"stream_options": {"include_usage": True}}
        if options.get("reasoning_effort"):
            body["reasoning_effort"] = options["reasoning_effort"]
        return body
