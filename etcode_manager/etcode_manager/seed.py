"""cloud-init NoCloud seed generation (user-data / meta-data on a CIDATA ISO).

Per PLAN.md each VM gets a small read-only ISO labelled ``CIDATA`` with:

* ``user-data``   — the cloud-config (user ``agent``, key-based SSH, the settings for the desktop app, ...)
* ``meta-data``   — a persistent ``instance-id`` and the hostname

The instance id is stored in the VM's ``vm.json`` so it is retained across
boots of the same VM (cloud-init must not re-run first-boot actions).

The seed can contain the backend access token (``write_files``), so the ISO is created with mode 0600.
Because cloud-init runs ``write_files`` once per instance id, changing the token in the manager does not reach
a VM that already booted: the user changes it in the desktop app's connection dialog instead.
"""

from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path

import pycdlib

USER_DATA_HEAD = """\
#cloud-config
users:
  - name: agent
    shell: /bin/bash
    lock_passwd: true
    sudo: ALL=(ALL) NOPASSWD:ALL
"""

USER_DATA_TAIL = """\
ssh_pwauth: false
disable_root: true

growpart:
  mode: auto
  devices: ["/"]
resize_rootfs: true
"""

# Where the desktop app in the guest looks for the values that the manager provisions
# (``etcode_desktop.paths.DEFAULT_PROVISIONED_FILE``).
DESKTOP_CONFIG_PATH = "/etc/etcode/desktop.json"


def render_desktop_json(backend_url: str = "", backend_token: str = "") -> str:
    """The content of ``/etc/etcode/desktop.json``: one line of JSON with the keys the desktop app reads.

    Keys without a value are left out, so that the app falls back to its next layer for them.
    """
    values = {}
    if backend_url.strip():
        values["backend_url"] = backend_url.strip()
    if backend_token.strip():
        values["token"] = backend_token.strip()
    if not values:
        return ""
    # ensure_ascii and a single line: nothing in a value can break out of the YAML block it is put in.
    return json.dumps(values, ensure_ascii=True)


def render_write_files(backend_url: str = "", backend_token: str = "") -> str:
    """The cloud-config ``write_files`` section that delivers the desktop settings, or "" if there are none.

    * ``defer: true`` writes the file late in boot, when the user ``agent`` certainly exists.
    * The file belongs to ``agent`` (mode 0600), so the app can read it. The app never writes into ``/etc/etcode``.
    * The JSON is one line, so indenting it into the block scalar cannot change it.
    """
    content = render_desktop_json(backend_url, backend_token)
    if not content:
        return ""
    return (
        "write_files:\n"
        f"  - path: {DESKTOP_CONFIG_PATH}\n"
        "    owner: agent:agent\n"
        '    permissions: "0600"\n'
        "    defer: true\n"
        "    content: |\n"
        f"      {content}\n"
    )


def render_user_data(
    ssh_authorized_keys: tuple[str, ...] = (),
    backend_url: str = "",
    backend_token: str = "",
) -> str:
    """Render the ``user-data`` cloud-config for one VM.

    Built by concatenation (not ``str.format``), because the token may contain braces. Keys are written as
    JSON strings, which are valid YAML double-quoted scalars.
    """
    keys = [key.strip() for key in ssh_authorized_keys if key.strip()]
    ssh_block = ""
    if keys:
        ssh_block = "    ssh_authorized_keys:\n" + "".join(f"      - {json.dumps(key)}\n" for key in keys)
    write_files = render_write_files(backend_url, backend_token)
    return USER_DATA_HEAD + ssh_block + USER_DATA_TAIL + ("\n" + write_files if write_files else "")


def render_meta_data(instance_id: str, hostname: str) -> str:
    """Render the ``meta-data`` file for one VM."""
    return f"instance-id: {instance_id}\nlocal-hostname: {hostname}\n"


def write_seed_iso(
    path: Path,
    instance_id: str,
    hostname: str,
    ssh_authorized_keys: tuple[str, ...] = (),
    backend_url: str = "",
    backend_token: str = "",
) -> Path:
    """Create a NoCloud ``CIDATA`` seed ISO at *path* (mode 0600) and return it."""
    files = {
        "user-data": render_user_data(ssh_authorized_keys, backend_url, backend_token).encode("utf-8"),
        "meta-data": render_meta_data(instance_id, hostname).encode("utf-8"),
    }

    iso = pycdlib.PyCdlib()
    iso.new(vol_ident="CIDATA", rock_ridge="1.09")
    for name, data in files.items():
        iso.add_fp(
            BytesIO(data),
            len(data),
            iso_path=f"/{name.replace('-', '').upper()}.;1",
            rr_name=name,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create the file private first: pycdlib then truncates and fills it without changing the mode.
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    try:
        os.chmod(path, 0o600)  # an existing file keeps its old mode otherwise
    except OSError:
        pass  # not every file system has modes (Windows)
    iso.write(str(path))
    iso.close()
    return path
