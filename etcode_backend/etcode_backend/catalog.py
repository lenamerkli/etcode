"""Curated, hard-coded model catalog."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

ProviderName = Literal["arena", "openrouter"]


@dataclass(frozen=True)
class ModelInfo:
    name: str
    tech_name: str
    context_length: int
    supports_images: bool
    provider: ProviderName
    provider_tech_name: str
    # OpenRouter only: pin the upstream provider (None = let OpenRouter route).
    openrouter_internal_provider: str | None = None
    # Optional fallback pricing in USD per million tokens, used only when the provider
    # does not report a cost itself. Leave None if unknown (cost is then "unpriced").
    price_input_per_mtok: str | None = None
    price_output_per_mtok: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("price_input_per_mtok", "price_output_per_mtok"):
            if data[key] is None:
                data.pop(key)
        if self.provider != "openrouter":
            data.pop("openrouter_internal_provider")
        return data


MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        name="Arena Coding Router",
        tech_name="arena-coding-router",
        context_length=1_000_000,
        supports_images=True,
        provider="arena",
        provider_tech_name="coding-router-preview",
    ),
    ModelInfo(
        name="Gemini 4 Argon",
        tech_name="gemini-4-argon",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="gemini-4-argon",
    )
)

_BY_TECH_NAME = {m.tech_name: m for m in MODELS}
assert len(_BY_TECH_NAME) == len(MODELS), "duplicate tech_name in catalog"


def get_model(tech_name: str) -> ModelInfo | None:
    return _BY_TECH_NAME.get(tech_name)
