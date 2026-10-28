from __future__ import annotations

from ..config import Settings
from .arena import ArenaProvider
from .base import Cancelled, CancelToken, OpenAICompatProvider, StreamEvent
from .openrouter import OpenRouterProvider


def get_provider(name: str, settings: Settings) -> OpenAICompatProvider:
    providers = {"arena": ArenaProvider, "openrouter": OpenRouterProvider}
    return providers[name](settings)


__all__ = ["get_provider", "Cancelled", "CancelToken", "StreamEvent", "OpenAICompatProvider"]
