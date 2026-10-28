"""Brave Search web API provider. No dependencies on the LLM adapters."""
from __future__ import annotations

import requests

from ..config import Settings
from . import SearchQuery


class BraveSearch:
    def __init__(self, settings: Settings):
        self.settings = settings

    def search(self, query: SearchQuery) -> list[dict[str, str | None]]:
        if not self.settings.brave_api_key:
            raise RuntimeError("Brave Search API key is not configured")
        params = {
            "q": query.query, "count": query.count, "offset": query.offset,
            "country": query.country, "search_lang": query.search_lang,
        }
        if query.freshness:
            params["freshness"] = query.freshness
        response = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={"Accept": "application/json", "X-Subscription-Token": self.settings.brave_api_key},
            timeout=self.settings.search_timeout,
        )
        response.raise_for_status()
        return [
            {"title": item.get("title", ""), "url": item.get("url", ""),
             "description": item.get("description", ""), "age": item.get("age")}
            for item in response.json().get("web", {}).get("results", [])
        ]
