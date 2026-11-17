"""Executors for tools that run on the backend (``websearch`` and future backend tools).

The backend no longer runs these tools by itself when a generation finishes. The client
runs every call of an assistant message in the order the model gave them and asks the
backend to execute the backend-owned ones through the tool-call endpoint. That keeps
client tools and backend tools in one strict order.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .config import Settings
from .search import SearchQuery, get_search_provider

log = logging.getLogger("etcode.backend_tools")

MAX_SEARCH_RESULT_CHARS = 100_000


def execute_backend_tool(name: str, arguments: dict[str, Any], *, websearch_enabled: bool,
                         settings: Settings) -> str | list[dict[str, Any]]:
    """Run one backend-owned tool and return its result (text, or content parts).

    Errors are results, not exceptions: the session barrier must always be able to move on.
    """
    if name == "websearch":
        return _run_search(arguments, websearch_enabled, settings)
    raise ValueError(f"unknown backend tool '{name}'")


def _run_search(arguments: dict[str, Any], enabled: bool, settings: Settings) -> str:
    if not enabled:
        return "Error: websearch is not enabled for this session."
    try:
        query = SearchQuery.model_validate(arguments)
        results = get_search_provider(settings).search(query)
        text = json.dumps(results, ensure_ascii=False, indent=1)
        return text[:MAX_SEARCH_RESULT_CHARS]
    except Exception as exc:  # noqa: BLE001 - reported to the model as a tool result
        log.warning("websearch failed: %s", exc)
        detail = str(exc) if exc.__class__.__name__ in ("ValidationError", "RuntimeError") else type(exc).__name__
        return f"Error: web search failed ({detail[:500]})"
