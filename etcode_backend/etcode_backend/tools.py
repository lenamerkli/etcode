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
            "description": "Read the contents of a file. If both start_line and start_char are provided, the one further from the start will be used. If both end_line and end_char are provided, the one further from the end will be used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path to the file to read"},
                    "start_line": {"type": "integer", "description": "The line to start reading from, 1-indexed", "default": 1},
                    "end_line": {"type": "integer", "description": "The line to end reading at", "default": 1000},
                    "max_chars": {"type": "integer", "description": "The maximum number of characters to read", "default": 1000000},
                    "start_char": {"type": "integer", "description": "The character to start reading from, 0-indexed", "default": 0},
                    "end_char": {"type": "integer", "description": "The character to end reading at", "default": 100000},
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

BACKEND_TOOLS = frozenset({"websearch"})  # tools executed by the backend itself


def tools_for(websearch: bool, supports_images: bool) -> list[dict[str, Any]]:
    tools = copy.deepcopy(DEFAULT_TOOLS)
    if websearch:
        tools.append(copy.deepcopy(WEBSEARCH_TOOL))
    if supports_images:
        tools.append(copy.deepcopy(VIEWIMAGE_TOOL))
    return tools


def tool_names(tools: list[dict[str, Any]]) -> set[str]:
    return {t["function"]["name"] for t in tools}
