"""The schemas of the tools that this client runs: a copy of the backend's definitions.

The backend (``etcode_backend/tools.py``) is the source. It tells the model what each tool takes,
and this copy tells the client how to check the arguments the model sends.
``tests/test_tool_schemas.py`` fails if the two differ, so they change together.

``websearch`` is not here: the backend runs it itself.
"""
from __future__ import annotations

from typing import Any

CLIENT_TOOLS: list[dict[str, Any]] = [{'name': 'bash',
  'description': 'Execute a bash shell command.',
  'parameters': {'type': 'object',
                 'properties': {'command': {'type': 'string', 'description': 'The command to execute'},
                                'timeout': {'type': 'integer',
                                            'description': 'The timeout for the command in seconds',
                                            'default': 60},
                                'directory': {'type': 'string',
                                              'description': 'The working directory to execute the command '
                                                             'in',
                                              'default': '/home/agent'},
                                'venv': {'type': 'string',
                                         'description': 'The path to the python virtual environment to '
                                                        'activate before executing the command'},
                                'max_chars': {'type': 'integer',
                                              'description': 'The maximum number of characters of output. It '
                                                             'will cut off the entire tool response, not '
                                                             'just stdout.',
                                              'default': 100000}},
                 'required': ['command']}},
 {'name': 'read_file',
  'description': 'Read the contents of a file. Reading starts at start_line or start_char, whichever comes '
                 'later, and stops at whichever of end_line, end_char or max_chars is reached first. '
                 'Parameters you omit use their defaults, so a call with only a path returns at most the '
                 'first 1000 lines and the first 100000 characters; to read more of a large file, set '
                 'end_line and end_char explicitly. The result reports which part of the file was read '
                 '(first_char, last_char, first_line, last_line) and how much was left out, before and after '
                 'that part (unread_chars, unread_lines).',
  'parameters': {'type': 'object',
                 'properties': {'path': {'type': 'string', 'description': 'The path to the file to read'},
                                'start_line': {'type': 'integer',
                                               'default': 1,
                                               'description': 'The line to start reading from, 1-indexed. If '
                                                              'start_char is later in the file, start_char '
                                                              'is used instead.'},
                                'end_line': {'type': 'integer',
                                             'default': 1000,
                                             'description': 'The last line to read, inclusive and 1-indexed. '
                                                            'Reading stops at the end of this line or at '
                                                            'end_char, whichever comes first.'},
                                'max_chars': {'type': 'integer',
                                              'default': 1000000,
                                              'description': 'The maximum number of characters to read, '
                                                             'counted from the start position.'},
                                'start_char': {'type': 'integer',
                                               'default': 0,
                                               'description': 'The character offset to start reading from, '
                                                              '0-indexed. If start_line begins later in the '
                                                              'file, start_line is used instead.'},
                                'end_char': {'type': 'integer',
                                             'default': 100000,
                                             'description': 'The character offset at which reading stops: an '
                                                            'absolute, 0-indexed position in the file, '
                                                            'exclusive. Reading stops here or at the end of '
                                                            'end_line, whichever comes first. The default of '
                                                            '100000 means that a read starting after '
                                                            'character 100000 returns nothing unless this is '
                                                            'raised.'}},
                 'required': ['path']}},
 {'name': 'write_to_file',
  'description': 'Write contents to a file. The file will be newly created or completely overwritten.',
  'parameters': {'type': 'object',
                 'properties': {'path': {'type': 'string',
                                         'description': 'The path to the file to create or overwrite'},
                                'content': {'type': 'string', 'description': 'The content to write'}},
                 'required': ['path', 'content']}},
 {'name': 'replace_in_file',
  'description': 'This is the main method to edit files.',
  'parameters': {'type': 'object',
                 'properties': {'path': {'type': 'string', 'description': 'The path to the file to edit'},
                                'search': {'type': 'string',
                                           'description': 'The content to replace (must match exactly, no '
                                                          'regex search)'},
                                'replace': {'type': 'string', 'description': 'The content to write'},
                                'read': {'type': 'boolean',
                                         'description': 'Whether to read the file in its entirety after the '
                                                        'replacement has taken place'}},
                 'required': ['path', 'search', 'replace']}},
 {'name': 'interactive_terminal',
  'description': 'Instead of just running a command and getting stdout and stderr, this tool allows for all '
                 'functionality of an interactive terminal including TUIs.',
  'parameters': {'type': 'object',
                 'properties': {'session': {'type': 'string',
                                            'description': 'A terminal ID scoped to this chat. A new ID '
                                                           'starts a terminal; reuse it to interact with '
                                                           'that terminal. IDs cleaned up after an app '
                                                           'restart cannot be reused.'},
                                'actions': {'type': 'string',
                                            'description': 'A comma-separated list of terminal actions to '
                                                           'execute sequentially. See '
                                                           '`/home/agent/skills/interactive_terminal.md` for '
                                                           'syntax.'},
                                'height': {'type': 'integer',
                                           'description': 'Terminal height in rows (1-200). Defaults to 50 '
                                                          'for a new session and preserves the current size '
                                                          'otherwise.'},
                                'width': {'type': 'integer',
                                          'description': 'Terminal width in columns (1-400). Defaults to 160 '
                                                         'for a new session and preserves the current size '
                                                         'otherwise.'}},
                 'required': ['session', 'actions']}},
 {'name': 'save_messages',
  'description': "Save the current chat's committed history as text, images, and tool-call JSON files "
                 'inside the VM.',
  'parameters': {'type': 'object',
                 'properties': {'directory': {'type': 'string',
                                              'description': 'A new directory to create. Relative paths are '
                                                             'relative to the VM home directory. Existing paths '
                                                             'are not overwritten.'}},
                 'required': ['directory']}},
 {'name': 'view_image',
  'description': 'View a png, jpeg or webp image.',
  'parameters': {'type': 'object',
                 'properties': {'path': {'type': 'string', 'description': 'The path to the image to view'}},
                 'required': ['path']}}]

CLIENT_TOOLS.append({
    "name": "interactive_gui",
    "description": (
        "Inspect and control windows on the VM desktop by their wmctrl -l IDs. Launch apps with bash. "
        "List windows first, then capture screenshots or send input. See /home/agent/skills/interactive_gui.md."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_windows", "screenshot", "click", "scroll", "type", "press", "hold", "move_relative"],
                "description": "The action to perform. Only list_windows does not need a window_id.",
            },
            "window_id": {"type": "string", "description": "Hex ID from list_windows (wmctrl -l)."},
            "x": {"type": "integer", "description": "Click x in client-area pixels, from the top left of the screenshot."},
            "y": {"type": "integer", "description": "Click y in client-area pixels, from the top left of the screenshot."},
            "button": {"type": "integer", "description": "Click button: 1 left, 2 middle, 3 right.", "default": 1},
            "ticks": {"type": "integer", "description": "Scroll wheel ticks: negative up, positive down (1 to 10 in either direction)."},
            "text": {"type": "string", "description": "Literal text to type, up to 256 characters."},
            "key": {"type": "string", "description": "X11 key name or chord, e.g. w, Escape, or ctrl+l."},
            "duration_ms": {"type": "integer", "description": "Duration of a hold (1 to 2000 milliseconds)."},
            "dx": {"type": "integer", "description": "Horizontal mouse delta (-500 to 500)."},
            "dy": {"type": "integer", "description": "Vertical mouse delta (-500 to 500)."},
            "steps": {
                "type": "integer", "default": 1,
                "description": "Repeat the relative delta this many times, at 50 ms intervals (1 to 20).",
            },
        },
        "required": ["action"],
    },
})
