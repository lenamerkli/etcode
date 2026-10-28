from __future__ import annotations

from ..catalog import ModelInfo
from ..errors import ProviderError
from .base import OpenAICompatProvider


class OpenRouterProvider(OpenAICompatProvider):
    name = "openrouter"

    def configured(self) -> bool:
        return bool(self.settings.openrouter_api_key)

    def endpoint(self) -> str:
        return self.settings.openrouter_base_url.rstrip("/") + "/chat/completions"

    def headers(self) -> dict[str, str]:
        if not self.settings.openrouter_api_key:
            raise ProviderError("not_configured", "OPENROUTER_API_KEY is not configured")
        headers = {"Authorization": f"Bearer {self.settings.openrouter_api_key}", "Content-Type": "application/json"}
        if self.settings.openrouter_referer:
            headers["HTTP-Referer"] = self.settings.openrouter_referer
        if self.settings.openrouter_title:
            headers["X-OpenRouter-Title"] = self.settings.openrouter_title
        return headers

    def extra_body(self, model: ModelInfo, options: dict) -> dict:
        body: dict = {}  # usage (incl. cost) is always included by OpenRouter
        if options.get("reasoning_effort"):
            body["reasoning"] = {"effort": options["reasoning_effort"]}
        if model.openrouter_internal_provider:
            body["provider"] = {"order": [model.openrouter_internal_provider], "allow_fallbacks": False}
        return body
