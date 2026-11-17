# etcode-manager

A desktop app that manages the VMs in which etcode agents perform tasks.
It runs on **Windows, Fedora and Debian** and drives QEMU directly — the
guest needs **no extra software** (no freeRDP/xrdp/VNC server inside the VM).

## Features (MVP)

* **Create VMs** — writable QCOW2 overlay per VM backed by the immutable
  `out/etcode-base-v1-amd64.qcow2` template, plus a cloud-init NoCloud
  `CIDATA` seed ISO (see `PLAN.md`).
* **Start / stop** — QEMU is launched detached with a QMP control socket;
  stop sends an ACPI powerdown (with a hard fallback).
* **View the screen** — a pure-Python RFB (VNC) client renders QEMU's
  built-in VNC display inside the app. The virtual display advertises
  **1920×1080** as its preferred resolution by default.
* **etcode desktop app in the VM** — the image contains the etcode desktop app (the
  GUI for the etcode backend), and it starts by itself when the VM's session starts. Each
  new VM gets the backend URL and access token through its cloud-init seed (see below).
* **Mouse and keyboard input** — clicks, wheel and keys are forwarded to
  the guest (X11 keysym translation included).

## Requirements

* Python 3.10+ (`pip install -r requirements.txt` → PySide6, pycdlib, Pillow)
* QEMU: `apt-get install qemu-system-x86 qemu-utils` (Debian),
  `dnf install qemu-kvm` (Fedora), [qemu.weilnetz.de](https://qemu.weilnetz.de/w64/) (Windows)
* The base image: run `./build_image.sh` (see `PLAN.md`), or point
  `base_image` in `config.json` at an existing QCOW2 template.

## Run

```bash
python -m etcode_manager
```

## Screen scaling and text quality

The **Scaling** selector next to the screen connection buttons switches between:

* **Smooth (area)** — the default. Uses Qt's area-averaged reduction to avoid dropping thin
  glyph strokes when the 1920×1080 desktop is fitted into a much smaller pane. Enlargement
  uses Qt's smooth interpolation.
* **Sharper (Lanczos)** — uses Pillow's antialiased Lanczos-3 filter for crisper text and
  edges. It costs more CPU per changing frame and can introduce faint halos; choose Smooth
  if you prefer softer edges or are viewing fast-moving content. The selection lasts for
  the current manager window and does not change the guest's resolution.

Both modes resize the **original framebuffer directly to the physical output pixel size**,
including fractional display scaling (125%, 150%, etc.). The result and letterbox position
are device-pixel aligned, then painted without a second resize. An unchanged frame is cached
across repaints; a new frame, target size, display pixel ratio or filter rebuilds that cache.
At native pixel size neither filter modifies the image. Mouse coordinates follow the same
rounded display rectangle.

After upgrading, run `pip install -r requirements.txt` to install Pillow for the Sharper mode.
The default area filter itself uses only Qt. These are **host-side viewer changes**: no VM image
rebuild or extra guest software is needed. Neither filter can recover detail once text becomes
only a few pixels high; enlarging the window or lowering the resolution in the guest's Display
settings can still help on small screens.

## Configuration (`config.json`, optional)

```json
{
  "base_image": "/path/to/etcode-base-v1-amd64.qcow2",
  "vms_dir": "/path/to/vms",
  "ssh_public_key": "ssh-ed25519 AAAA... you@host",
  "backend_url": "http://10.0.2.2:8765",
  "backend_token": "the access token for etcode_backend"
}
```

`ssh_public_key` is baked into each new VM's cloud-init `user-data` so you
can SSH into the `agent` user. `backend_url` and `backend_token` are the defaults for the
etcode backend that the desktop app in a VM talks to (see "The etcode desktop app in the VM").
The VMs are stored as:

```
vms/<instance-id>/
    disk.qcow2   writable overlay (backed by the template)
    seed.iso     cloud-init NoCloud seed (retains its instance-id), mode 0600
    vm.json      name, resources, ports, backend URL and token, mode 0600
    qemu.log     QEMU console output
```

## The etcode desktop app in the VM

The image has the etcode desktop app (`../etcode_desktop`) in `/opt/etcode/etcode_desktop`, with its
dependencies (PySide6, requests) in `/opt/etcode/.venv`. `/usr/local/bin/etcode-desktop` starts it, it has an
entry in the application menu and a quick-launch button on the panel (so it can be opened again after it was
closed), and `/etc/xdg/autostart/etcode-desktop.desktop` starts it maximized when the session starts, two
seconds after the panel. The manager does not launch it.

**Backend URL and token.** The *New VM* dialog has the fields *Backend URL* and *Backend token*, prefilled from
`backend_url` and `backend_token` in `config.json` (default URL `http://10.0.2.2:8765`: the host, as seen from
a VM behind QEMU's user-mode network; a server is just another URL). Change them per VM to give one VM its own
token. An empty URL means the default, an empty token means that the app asks for one when it starts.
The values are stored in the VM's `vm.json` (the seed needs them whenever it is regenerated), and `seed.py`
delivers them with a cloud-init `write_files` entry to `/etc/etcode/desktop.json` in the VM (owner `agent`,
mode 0600). The app reads that file as its "provisioned" layer.

* **Changes do not reach a VM that has booted.** cloud-init writes the file once per instance id, and the
  manager keeps the instance id stable. To change the token of an existing VM, use the connection dialog of
  the desktop app (the gear in its status bar). A value set there wins over the provisioned one.
* **The token is a secret.** It is in `vm.json` and `seed.iso` on your disk (both mode 0600), and in the VM
  in `/etc/etcode/desktop.json`. **The model can read it**: it runs commands as `agent`, who can also use
  `sudo`. A backend token only reaches the sessions of its own owner, so use a token that is only for etcode VMs
  (one per VM when sessions must be kept apart). Never put a token into the template image: the build scripts
  contain none, and a test checks that.
* **Use HTTPS** for a backend that is not on your own machine or network. The desktop app warns about `http://`
  to a public address.
* The first boot can reach the desktop before cloud-init has written the file. The autostart entry therefore
  runs `etcode-desktop --wait-for-provisioning`, which waits (at most 60 s) until the file exists or cloud-init
  has finished.

**Rebuilding the image.** The desktop app is part of the template, so an existing template does not have it:
build a new one (`IMAGE_VERSION=v5 ./build_image.sh`, because templates are versioned and never modified), then
point `base_image` at it. VMs that were made from an older template keep that template. `build_image.sh` takes
the app from `../etcode_desktop` (or `DESKTOP_DIR`), and `provision.sh` installs its dependencies and checks,
with `ldd` and an offscreen start, that Qt can load. A build fails loudly if a system library is missing.

## Tests

```bash
python -m unittest discover -s tests
```

`tests/test_desktop_provisioning.py` covers the backend settings, the `write_files` section of the seed (valid YAML for
any token, and the JSON is what the desktop app reads), the private files, the dialog, and that the image scripts,
packaging files and panel layout fit together. `tests/test_scaling.py` covers area/Lanczos filtering, aliasing,
cache invalidation and input mapping. It also checks actual offscreen painting in subprocesses at 100%, 125%, 150%
and 200% display scaling: the displayed pixels must exactly match a single resampling of the original frame.
`tests/test_gui_smoke.py` checks the scaling selector and other screen controls.
`tests/test_rfb.py` validates the RFB client against a fake VNC server;
`tests/test_vm_lifecycle.py` runs create → start → QMP → VNC → stop →
delete against real QEMU (skipped if QEMU is not installed).

## Design notes

* Screen + input use **QEMU's built-in VNC server** on loopback (RFB 3.8,
  raw encoding, 32 bpp). This keeps the template image untouched and works
  identically with KVM (Fedora/Debian), WHPX (Windows) and TCG (fallback).
* VM control uses **QMP over TCP loopback** (portable across the three
  target OSes; no Unix sockets on Windows).
* The GUI is **PySide6**; the RFB client is dependency-free Python, so the
  protocol layer is testable headlessly.