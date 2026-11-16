"""Inference options of a session: temperature, the most tokens to write, and how hard the model should think.

They are chosen when a session is created (the backend keeps them) and can be given again with each message, which
is how the window changes them later: the choice is remembered per session and sent as overrides with every message.
``None`` means "the provider's default".
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
MAX_TEMPERATURE = 2.0


@dataclass(frozen=True)
class SessionOptions:
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if self.temperature is not None and not (isinstance(self.temperature, (int, float)) and not isinstance(self.temperature, bool)
                                                 and 0 <= self.temperature <= MAX_TEMPERATURE):
            raise ValueError(f"the temperature must be between 0 and {MAX_TEMPERATURE:g}")
        if self.max_tokens is not None and (isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int) or self.max_tokens < 1):
            raise ValueError("the most tokens to write must be a whole number of at least 1")
        if self.reasoning_effort is not None and self.reasoning_effort not in EFFORTS:
            raise ValueError(f"the reasoning effort must be one of: {', '.join(EFFORTS)}")

    @property
    def is_default(self) -> bool:
        return self.temperature is None and self.max_tokens is None and self.reasoning_effort is None

    def overrides(self) -> dict[str, Any]:
        """What goes with a message: only what was chosen."""
        return {k: v for k, v in (("temperature", self.temperature), ("max_tokens", self.max_tokens),
                                  ("reasoning_effort", self.reasoning_effort)) if v is not None}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> SessionOptions:
        """From what the backend reports for a session (or from storage). Values that do not fit are ignored."""
        data = data or {}
        values: dict[str, Any] = {}
        for key in ("temperature", "max_tokens", "reasoning_effort"):
            candidate = data.get(key)
            if candidate is None:
                continue
            try:
                cls(**{key: candidate})
            except (ValueError, TypeError):
                continue
            values[key] = candidate
        return cls(**values)

    def to_json(self) -> str:
        return json.dumps(self.overrides(), sort_keys=True)

    @classmethod
    def from_json(cls, text: str | None) -> SessionOptions:
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        return cls.from_mapping(data if isinstance(data, dict) else {})
