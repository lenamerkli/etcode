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
  built-in VNC display inside the app.
* **Mouse and keyboard input** — clicks, wheel and keys are forwarded to
  the guest (X11 keysym translation included).

## Requirements

* Python 3.10+ (`pip install -r requirements.txt` → PySide6, pycdlib)
* QEMU: `apt-get install qemu-system-x86 qemu-utils` (Debian),
  `dnf install qemu-kvm` (Fedora), [qemu.weilnetz.de](https://qemu.weilnetz.de/w64/) (Windows)
* The base image: run `./build_image.sh` (see `PLAN.md`), or point
  `base_image` in `config.json` at an existing QCOW2 template.

## Run

```bash
python -m etcode_manager
```

## Configuration (`config.json`, optional)

```json
{
  "base_image": "/path/to/etcode-base-v1-amd64.qcow2",
  "vms_dir": "/path/to/vms",
  "ssh_public_key": "ssh-ed25519 AAAA... you@host"
}
```

`ssh_public_key` is baked into each new VM's cloud-init `user-data` so you
can SSH into the `agent` user. The VMs are stored as:

```
vms/<instance-id>/
    disk.qcow2   writable overlay (backed by the template)
    seed.iso     cloud-init NoCloud seed (retains its instance-id)
    vm.json      name, resources, ports
    qemu.log     QEMU console output
```

## Tests

```bash
python -m unittest discover -s tests
```

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