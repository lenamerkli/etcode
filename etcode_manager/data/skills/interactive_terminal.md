# Interactive Terminal Actions

`interactive_terminal` provides a persistent PTY for interactive shells and TUIs. A terminal ID is scoped to the current chat. Reuse the same ID to keep interacting with that terminal; use a different ID to start another terminal. Sessions live only while the desktop app process is running. After an app restart, old IDs are deliberately invalid: use a new ID, and the tool will explain that the old terminal was cleaned up.

For `actions`, write comma-separated calls, for example:

```text
type("watch -n 1 nvidia-smi"),press("ENTER"),wait(1),print(),press("CTRL+C"),
```

You may leave a trailing comma. Actions run sequentially, and each tool call returns the screen at each `print()`; if there is no `print()`, it returns the final screen.

## `type`

Type a JSON string of text into the terminal:

```text
type("echo \"hello\""),press("ENTER"),
```

JSON string escaping is supported. `type` does not press Enter automatically.

## `press`

Send a special key or key combination:

```text
press("ENTER"),press("CTRL+C"),press("ARROWDOWN"),
```

Names: `ENTER`, `TAB`, `BACKSPACE`, `DELETE`, `ESC`, `SPACE`, `ARROWUP`, `ARROWDOWN`, `ARROWLEFT`, `ARROWRIGHT`, `HOME`, `END`, `PAGEUP`, `PAGEDOWN`, `INSERT`, `F1` through `F12`, and modifiers `CTRL`, `ALT`, `ALTGR`, `SHIFT`, `SUPER`. Use `+` for combinations, for example `CTRL+C`; use `PLUS` for a literal plus key. A single printable character is also accepted.

## `wait`

Wait for terminal output for a number of seconds:

```text
wait(1.5),
```

Each wait is limited to 300 seconds and total waiting in a call to 600 seconds.

## `print`

Return the visible terminal screen, without color information:

```text
print(),
```

To preserve terminal colors and text styles, use:

```text
print(color),
```

Terminal dimensions default to 50 rows by 160 columns. They can be set or changed using `height` (1-200) and `width` (1-400).
