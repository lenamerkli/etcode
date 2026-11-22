"""Run the tool calls that the model makes, including bash and persistent PTY terminals.

:func:`execute_tool` is the one entry point. It checks the arguments against the tool's schema, runs the
tool, and always returns content parts for the tool result. It never raises, because the session waits for a
result for every call: a bad call, a failing command or a bug in a tool all become an ``Error: ...`` text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..api import BackendClient
from ..models import text_part
from .bash import run_bash
from .cancel import CancelToken
from .files import FileToolError, read_file, replace_in_file, resolve_path, write_to_file
from .image import view_image
from .interactive_terminal import TerminalManager
from .save_messages import export_messages
from .schemas import TOOL_NAMES, ToolArgumentError, explicit, validate_arguments

log = logging.getLogger(__name__)

__all__ = ["CancelToken", "ToolContext", "execute_tool", "TOOL_NAMES", "error_text"]


@dataclass
class ToolContext:
    client: BackendClient | None = None  # used by view_image and save_messages
    cancel: CancelToken = field(default_factory=CancelToken)
    home: Path = field(default_factory=Path.home)  # relative paths are relative to this
    terminal_manager: TerminalManager | None = None
    chat_id: str = "direct"
    backend_url: str = "local"


def error_text(message: str) -> list[dict[str, Any]]:
    return [text_part(f"Error: {message}")]


def execute_tool(name: str, arguments: dict[str, Any], context: ToolContext) -> list[dict[str, Any]]:
    """The content parts of the result of one tool call."""
    try:
        args = validate_arguments(name, arguments)
        return _run(name, args, arguments, context)
    except ToolArgumentError as exc:
        return error_text(f"invalid call of {name}: {exc}")
    except FileToolError as exc:
        return error_text(str(exc))
    except Exception as exc:  # a bug in a tool must still give the model an answer
        log.exception("the tool %s failed", name)
        return error_text(f"the tool {name} failed: {type(exc).__name__}: {exc}")


def _run(name: str, args: dict[str, Any], given: dict[str, Any], context: ToolContext) -> list[dict[str, Any]]:
    home = context.home
    if name == "bash":
        if args["timeout"] < 1:
            raise ToolArgumentError("timeout must be at least 1 second")
        if args["max_chars"] < 1:
            raise ToolArgumentError("max_chars must be at least 1")
        directory = resolve_path(args["directory"], home)
        if not explicit(given, "directory") and not directory.is_dir():
            directory = home  # the default directory belongs to the VM: elsewhere, the home directory will do
        venv = None
        if explicit(given, "venv"):
            venv = resolve_path(args["venv"], home)
            if not (venv / "bin").is_dir():
                raise FileToolError(f"the virtual environment {args['venv']} does not exist (no bin directory)")
        if not directory.is_dir():
            from .bash import format_result
            return [text_part(format_result(-1, f"cannot start the command: the directory {args['directory']} does not exist", ""))]
        return [text_part(run_bash(args["command"], args["timeout"], directory, venv, args["max_chars"], context.cancel))]
    if name == "interactive_terminal":
        manager = context.terminal_manager
        if manager is None:
            raise FileToolError("interactive terminals are unavailable in this tool context")
        result = manager.run(chat_id=context.chat_id, backend_url=context.backend_url,
                             terminal_id=args["session"], actions=args["actions"],
                             height=args.get("height"), width=args.get("width"), cancel=context.cancel)
        return [text_part(result)]
    if name == "read_file":
        return [text_part(read_file(args["path"], args["start_line"], args["end_line"], args["max_chars"],
                                    args["start_char"], args["end_char"], home))]
    if name == "write_to_file":
        return [text_part(write_to_file(args["path"], args["content"], home))]
    if name == "replace_in_file":
        return [text_part(replace_in_file(args["path"], args["search"], args["replace"], bool(args.get("read", False)), home))]
    if name == "save_messages":
        if context.client is None or context.chat_id in ("", "direct"):
            raise FileToolError("saving messages requires an active chat and a connection to the backend")
        result = export_messages(args["directory"], client=context.client, session_id=context.chat_id,
                                 home=home, cancel=context.cancel)
        return [text_part(result)]
    if name == "view_image":
        if context.client is None:
            raise FileToolError("images cannot be uploaded: there is no connection to the backend")
        return view_image(args["path"], home, context.client)
    raise ToolArgumentError(f"unknown tool '{name}'")  # unreachable: validate_arguments knows the names
