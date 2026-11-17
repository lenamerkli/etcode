"""Tool schemas advertised to the LLM (mirrors PLAN.md)."""
from __future__ import annotations

import copy
from typing import Any

DEFAULT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash shell command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to execute"},
                    "timeout": {"type": "integer", "description": "The timeout for the command in seconds", "default": 60},
                    "directory": {"type": "string", "description": "The working directory to execute the command in", "default": "/home/agent"},
                    "venv": {"type": "string", "description": "The path to the python virtual environment to activate before executing the command"},
                    "max_chars": {"type": "integer", "description": "The maximum number of characters of output. It will cut off the entire tool response, not just stdout.", "default": 100000},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file. Reading starts at start_line or start_char, whichever comes later, "
                "and stops at whichever of end_line, end_char or max_chars is reached first. "
                "Parameters you omit use their defaults, so a call with only a path returns at most the first "
                "1000 lines and the first 100000 characters; to read more of a large file, set end_line and "
                "end_char explicitly. The result reports which part of the file was read (first_char, last_char, "
                "first_line, last_line) and how much was left out, before and after that part "
                "(unread_chars, unread_lines)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path to the file to read"},
                    "start_line": {"type": "integer", "default": 1, "description": (
                        "The line to start reading from, 1-indexed. If start_char is later in the file, "
                        "start_char is used instead.")},
                    "end_line": {"type": "integer", "default": 1000, "description": (
                        "The last line to read, inclusive and 1-indexed. Reading stops at the end of this line "
                        "or at end_char, whichever comes first.")},
                    "max_chars": {"type": "integer", "default": 1000000, "description": (
                        "The maximum number of characters to read, counted from the start position.")},
                    "start_char": {"type": "integer", "default": 0, "description": (
                        "The character offset to start reading from, 0-indexed. If start_line begins later in "
                        "the file, start_line is used instead.")},
                    "end_char": {"type": "integer", "default": 100000, "description": (
                        "The character offset at which reading stops: an absolute, 0-indexed position in the "
                        "file, exclusive. Reading stops here or at the end of end_line, whichever comes first. "
                        "The default of 100000 means that a read starting after character 100000 returns "
                        "nothing unless this is raised.")},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_to_file",
            "description": "Write contents to a file. The file will be newly created or completely overwritten.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path to the file to create or overwrite"},
                    "content": {"type": "string", "description": "The content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "This is the main method to edit files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path to the file to edit"},
                    "search": {"type": "string", "description": "The content to replace (must match exactly, no regex search)"},
                    "replace": {"type": "string", "description": "The content to write"},
                    "read": {"type": "boolean", "description": "Whether to read the file in its entirety after the replacement has taken place"},
                },
                "required": ["path", "search", "replace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "interactive_terminal",
            "description": "Instead of just running a command and getting stdout and stderr, this tool allows for all functionality of an interactive terminal including TUIs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "A terminal ID scoped to this chat. A new ID starts a terminal; reuse it to interact with that terminal. IDs cleaned up after an app restart cannot be reused."},
                    "actions": {"type": "string", "description": "A comma-separated list of terminal actions to execute sequentially. See `/home/agent/skills/interactive_terminal.md` for syntax."},
                    "height": {"type": "integer", "description": "Terminal height in rows (1-200). Defaults to 50 for a new session and preserves the current size otherwise."},
                    "width": {"type": "integer", "description": "Terminal width in columns (1-400). Defaults to 160 for a new session and preserves the current size otherwise."}
                },
                "required": ["session", "actions"]
            }
        }
    }
]

WEBSEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "websearch",
        "description": "Search the web and return a list of web results with title, url, description and age.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query. Maximum 400 characters and 50 words."},
                "count": {"type": "integer", "description": "The number of search results returned. Maximum 20. The actual number delivered may be less.", "default": 10},
                "offset": {"type": "integer", "description": "The zero based page offset to skip before returning results. Used together with count to paginate. Maximum 9.", "default": 0},
                "freshness": {"type": "string", "description": "Filter results by page age: 'pd' (24 hours or less), 'pw' (7 days or less), 'pm' (31 days or less), 'py' (365 days or less), or a custom range like 'YYYY-MM-DDtoYYYY-MM-DD'"},
                "country": {"type": "string", "description": "The 2 character country code where the search results come from, e.g. 'US' or 'DE'", "default": "US"},
                "search_lang": {"type": "string", "description": "The language code for which the search results are provided, e.g. 'en' or 'de'", "default": "en"},
            },
            "required": ["query"],
        },
    },
}

VIEWIMAGE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "view_image",
        "description": "View a png, jpeg or webp image.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "The path to the image to view"}},
            "required": ["path"],
        },
    },
}

# Tools whose implementation lives on the backend. The client runs every call of a
# round in model order and asks the backend to execute these through
# POST /session/<id>/tool-calls/<call_id>/run. Add a tool here and to
# backend_tools.execute_backend_tool; clients need no change.
BACKEND_TOOLS = frozenset({"websearch"})


def tools_for(websearch: bool, supports_images: bool) -> list[dict[str, Any]]:
    tools = copy.deepcopy(DEFAULT_TOOLS)
    if websearch:
        tools.append(copy.deepcopy(WEBSEARCH_TOOL))
    if supports_images:
        tools.append(copy.deepcopy(VIEWIMAGE_TOOL))
    return tools


def tool_names(tools: list[dict[str, Any]]) -> set[str]:
    return {t["function"]["name"] for t in tools}
