"""Search service plugin registry. Register another provider in SEARCH_PROVIDERS."""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field, field_validator

from ..config import Settings


class SearchQuery(BaseModel):
    query: str = Field(min_length=1, max_length=400)
    count: int = Field(default=10, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=9)
    freshness: str | None = None
    country: str = Field(default="US", pattern=r"^[A-Za-z]{2}$")
    search_lang: str = Field(default="en", pattern=r"^[A-Za-z]{2}$")

    @field_validator("query")
    @classmethod
    def check_words(cls, value: str) -> str:
        if len(value.split()) > 50:
            raise ValueError("query may contain at most 50 words")
        return value

    @field_validator("freshness")
    @classmethod
    def check_freshness(cls, value: str | None) -> str | None:
        import re
        if value is not None and not re.fullmatch(r"pd|pw|pm|py|\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2}", value):
            raise ValueError("invalid freshness value")
        return value


class SearchProvider(Protocol):
    def search(self, query: SearchQuery) -> list[dict[str, str | None]]: ...


def get_search_provider(settings: Settings) -> SearchProvider:
    from .brave import BraveSearch

    providers = {"brave": BraveSearch}
    try:
        cls = providers[settings.search_provider]
    except KeyError as exc:
        raise ValueError(f"Unknown search provider: {settings.search_provider}") from exc
    return cls(settings)
