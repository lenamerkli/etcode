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
        name="Claude Haiku 5.5",
        tech_name="claude-haiku-5.5",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="anthropic/claude-haiku-5.5",
    ),
    ModelInfo(
        name="Claude Fable 5.2",
        tech_name="claude-fable-5.2",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="anthropic/claude-fable-5.2",
    ),
    ModelInfo(
        name="Claude Opus 5.5",
        tech_name="claude-opus-5.5",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="anthropic/claude-opus-5.5",
    ),
    ModelInfo(
        name="Claude Sonnet 5.5",
        tech_name="claude-sonnet-5.5",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="anthropic/claude-sonnet-5.5",
    ),
    ModelInfo(
        name="DeepSeek V4 Pro",
        tech_name="deepseek-v4-pro-0813",
        context_length=1_000_000,
        supports_images=False,
        provider="openrouter",
        provider_tech_name="deepseek/deepseek-v4-pro-0813",
    ),
    ModelInfo(
        name="DeepSeek V4.1 Flash",
        tech_name="deepseek-v4.1-flash-1017",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="deepseek/deepseek-v4.1-flash-1017",
        openrouter_internal_provider="together",
    ),
    ModelInfo(
        name="GLM 5.3 Flash",
        tech_name="glm-5.3-flash",
        context_length=1_300_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="z-ai/glm-5.3-flash",
    ),
    ModelInfo(
        name="GLM 5.3",
        tech_name="glm-5.3",
        context_length=1_300_000,
        supports_images=False,
        provider="openrouter",
        provider_tech_name="z-ai/glm-5.3",
    ),
    ModelInfo(
        name="GPT-6.2 Astra Pro",
        tech_name="gpt-6.2-astra-pro",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="openai/gpt-6.2-astra-pro",
    ),
    ModelInfo(
        name="GPT-6.2 Astra",
        tech_name="gpt-6.2-astra",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="openai/gpt-6.2-astra",
    ),
    ModelInfo(
        name="GPT-6 Luna",
        tech_name="gpt-6-luna",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="openai/gpt-6-luna",
    ),
    ModelInfo(
        name="GPT-6.2 Sol",
        tech_name="gpt-6.2-sol",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="openai/gpt-6.2-sol",
    ),
    ModelInfo(
        name="GPT-6.1 Terra",
        tech_name="gpt-6.1-terra",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="openai/gpt-6.1-terra",
    ),
    ModelInfo(
        name="Gemini 4 Argon",
        tech_name="gemini-4-argon",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="google/gemini-4-argon",
    ),
    ModelInfo(
        name="Gemini 4 Carbon",
        tech_name="gemini-4-carbon",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="google/gemini-4-carbon",
    ),
    ModelInfo(
        name="Gemini 4 Hydrogen",
        tech_name="gemini-4-hydrogen",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="google/gemini-4-hydrogen",
    ),
    ModelInfo(
        name="Gemini 4 Neon",
        tech_name="gemini-4-neon",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="google/gemini-4-neon",
    ),
    ModelInfo(
        name="Kimi K3.1 Pro",
        tech_name="kimi-k3.1-pro",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="moonshotai/kimi-k3.1-pro",
    ),
    ModelInfo(
        name="Ling 3.2 Flash (Free)",
        tech_name="ling-3.2-flash:free",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="inclusionai/ling-3.2-flash:free",
    ),
    ModelInfo(
        name="Mimo V2.6 Pro",
        tech_name="mimo-v2.6-pro",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="xiaomi/mimo-v2.6-pro",
    ),
    ModelInfo(
        name="Mimo V2.6",
        tech_name="mimo-v2.6",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="xiaomi/mimo-v2.6",
    ),
    ModelInfo(
        name="Minimax M3",
        tech_name="minimax-m3",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="minimax/minimax-m3",
    ),
    ModelInfo(
        name="Minimax M3.1 Flash",
        tech_name="minimax-m3.1-flash",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="minimax/minimax-m3.1-flash",
    ),
    ModelInfo(
        name="Muse Ember",
        tech_name="muse-ember",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="meta/muse-ember",
    ),
    ModelInfo(
        name="Muse Spark 1.3",
        tech_name="muse-spark-1.3",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="meta/muse-spark-1.3",
    ),
    ModelInfo(
        name="Muse Wonder",
        tech_name="muse-wonder",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="meta/muse-wonder",
    ),
    ModelInfo(
        name="Nemotron 3.5 Super VL (Free)",
        tech_name="nemotron-3.5-super-vl:free",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="nvidia/nemotron-3.5-super-vl:free",
    ),
    ModelInfo(
        name="Qwen 3.8 Flash",
        tech_name="qwen3.8-flash",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="qwen/qwen3.8-flash",
    ),
    ModelInfo(
        name="Qwen 4 Max",
        tech_name="qwen4-max",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="qwen/qwen4-max",
    ),
    ModelInfo(
        name="Solar Pro 4",
        tech_name="solar-pro4",
        context_length=520_000,
        supports_images=False,
        provider="openrouter",
        provider_tech_name="upstage/solar-pro4",
    ),
    ModelInfo(
        name="Tencent Hy4",
        tech_name="hy4",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="tencent/hy4",
    ),
    ModelInfo(
        name="Tencent Hy4 Flash",
        tech_name="hy4-flash",
        context_length=1_000_000,
        supports_images=True,
        provider="openrouter",
        provider_tech_name="tencent/hy4-flash",
    ),
)

_BY_TECH_NAME = {m.tech_name: m for m in MODELS}
assert len(_BY_TECH_NAME) == len(MODELS), "duplicate tech_name in catalog"
assert len({(m.provider, m.provider_tech_name) for m in MODELS}) == len(MODELS), \
    "duplicate (provider, provider_tech_name) in catalog"


def get_model(tech_name: str) -> ModelInfo | None:
    return _BY_TECH_NAME.get(tech_name)
