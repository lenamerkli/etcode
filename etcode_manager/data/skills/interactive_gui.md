# Interactive GUI (Xfce guest)

Use `bash` to launch apps on the guest desktop and `interactive_gui` to inspect and operate their windows. The tool is offered to image-capable models. It does not launch or own application processes.

1. Run `interactive_gui({"action":"list_windows"})` and find the application's hex `wmctrl -l` ID. Re-list after a window is closed or recreated, or after a reboot. Don't guess a replacement ID from an old screenshot.
2. Run `{"action":"screenshot","window_id":"0x02400003"}` to activate the window and upload its client-area screenshot to the chat. Coordinates are in that image's pixels, from the top left; don't use host viewer coordinates. Capture again after resizing or changing layouts.
3. Send one action per call, with that `window_id`. Inspect the result before taking another action if the UI may have changed.

| Action | Additional arguments |
| --- | --- |
| `click` | `x`, `y`, optional `button` (1 left, 2 middle, 3 right) |
| `scroll` | `ticks` (−10 to −1 for up, 1 to 10 for down), at the current pointer location inside the window |
| `type` | `text` (up to 256 characters, subject to the X11 keyboard layout) |
| `press` | `key` (X11 keysym or chord, e.g. `Return`, `Escape`, `ctrl+l`) |
| `hold` | `key`, `duration_ms` (1–2000); e.g. hold `w` for 600 ms |
| `move_relative` | `dx`, `dy` (each −500 to 500), optional `steps` (1–20, 50 ms apart); in a game, start with small deltas |

For a Fabric mod, run the project's Gradle `runClient` task with the Java version it requires. Start it in the background (for example, `./gradlew runClient >run-client.log 2>&1 </dev/null &`) and check the log if a window does not appear. Use a disposable local world. Enter it with clicks, walk with `hold`, turn with `move_relative`, then take a screenshot to check what actually happened. If the game doesn't respond to relative input, verify it has captured the mouse, and try testing with Minecraft's Raw Input setting toggled. Software rendering can be slow; reduce resolution or render distance if necessary.

The agent and the person using the VM viewer share this desktop. Run GUI actions only on the window the user wants controlled. Stop the chat's tool run before taking over; held keys are released on Stop and focus loss. The tool leaves the app open. There is no automatic handoff, and another input device can still change focus during an action. Screenshots are uploaded to the configured backend, so avoid windows that contain secrets or unrelated private content.

# What is the difference between actions and other properties?

In `interactive_gui`, **`action` says what to do**. The other properties say **which window to use and how to perform that action**.

```json
{"action": "list_windows"}
{"action": "screenshot", "window_id": "0x02400007"}
{"action": "hold", "window_id": "0x02400007", "key": "w", "duration_ms": 800}
{"action": "move_relative", "window_id": "0x02400007", "dx": 40, "dy": -10}
```

`list_windows` is the only action that doesn’t need a `window_id`. A screenshot needs only the ID, while a key hold also needs a key and a duration. You leave out properties that don’t apply.

Unlike `interactive_terminal`, which takes an **`actions`** string containing a sequence, `interactive_gui` takes **one structured `action` per call**. Walking, turning, and taking a screenshot are three separate calls.
