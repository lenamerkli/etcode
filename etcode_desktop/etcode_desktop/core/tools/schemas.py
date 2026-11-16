"""Check the arguments of a tool call against the tool's schema, and fill in the defaults.

A bad call is not an exception for the caller to handle. It becomes an ``Error: ...`` text that the model
reads, because the session waits for a result for every call.
"""
from __future__ import annotations

from typing import Any

from .schemas_data import CLIENT_TOOLS

SCHEMAS: dict[str, dict[str, Any]] = {tool["name"]: tool for tool in CLIENT_TOOLS}
TOOL_NAMES = frozenset(SCHEMAS)

_TYPE_NAMES = {"string": "a string", "integer": "an integer", "boolean": "true or false"}


class ToolArgumentError(Exception):
    """The model sent arguments that the tool cannot use. The message is meant for the model."""


class UnknownTool(ToolArgumentError):
    pass


def _coerce(name: str, value: Any, wanted: str) -> Any:
    if wanted == "string" and isinstance(value, str):
        return value
    if wanted == "boolean" and isinstance(value, bool):
        return value
    if wanted == "integer":
        if isinstance(value, bool):
            pass  # True is an int in Python, but not what the model means
        elif isinstance(value, int):
            return value
        elif isinstance(value, float) and value.is_integer():
            return int(value)  # JSON writes 60.0 for some models
    raise ToolArgumentError(f"argument '{name}' must be {_TYPE_NAMES.get(wanted, wanted)}")


def validate_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The arguments with defaults filled in. Unknown extra arguments are ignored. Raises ToolArgumentError."""
    schema = SCHEMAS.get(tool)
    if schema is None:
        raise UnknownTool(f"unknown tool '{tool}'. The tools that can be run here are: {', '.join(sorted(TOOL_NAMES))}")
    if "_invalid_arguments" in arguments:
        raw = str(arguments["_invalid_arguments"])
        raise ToolArgumentError(f"the arguments of {tool} were not valid JSON: {raw[:200]}")
    parameters = schema["parameters"]
    result: dict[str, Any] = {}
    for name in parameters.get("required", []):
        if name not in arguments or arguments[name] is None:
            raise ToolArgumentError(f"missing required argument '{name}'")
    for name, spec in parameters["properties"].items():
        if name in arguments and arguments[name] is not None:
            result[name] = _coerce(name, arguments[name], spec["type"])
        elif "default" in spec:
            result[name] = spec["default"]
    return result


def explicit(arguments: dict[str, Any], name: str) -> bool:
    """Did the model pass this argument itself (as opposed to the default being used)?"""
    return name in arguments and arguments[name] is not None
