"""cloud-init NoCloud seed generation (user-data / meta-data on a CIDATA ISO).

Per PLAN.md each VM gets a small read-only ISO labelled ``CIDATA`` with:

* ``user-data``   — the cloud-config (user ``agent``, key-based SSH, ...)
* ``meta-data``   — a persistent ``instance-id`` and the hostname

The instance id is stored in the VM's ``vm.json`` so it is retained across
boots of the same VM (cloud-init must not re-run first-boot actions).
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pycdlib

USER_DATA_TEMPLATE = """\
#cloud-config
users:
  - name: agent
    shell: /bin/bash
    lock_passwd: true
    sudo: ALL=(ALL) NOPASSWD:ALL
{ssh_block}
ssh_pwauth: false
disable_root: true

growpart:
  mode: auto
  devices: ["/"]
resize_rootfs: true
"""

SSH_BLOCK = """\
    ssh_authorized_keys:
{keys}"""


def render_user_data(ssh_authorized_keys: tuple[str, ...] = ()) -> str:
    """Render the ``user-data`` cloud-config for one VM."""
    keys = "\n".join(f'      - "{key.strip()}"' for key in ssh_authorized_keys if key.strip())
    ssh_block = SSH_BLOCK.format(keys=keys) if keys else ""
    return USER_DATA_TEMPLATE.format(ssh_block=ssh_block)


def render_meta_data(instance_id: str, hostname: str) -> str:
    """Render the ``meta-data`` file for one VM."""
    return f"instance-id: {instance_id}\nlocal-hostname: {hostname}\n"


def write_seed_iso(
    path: Path,
    instance_id: str,
    hostname: str,
    ssh_authorized_keys: tuple[str, ...] = (),
) -> Path:
    """Create a NoCloud ``CIDATA`` seed ISO at *path* and return it."""
    files = {
        "user-data": render_user_data(ssh_authorized_keys).encode("utf-8"),
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
    iso.write(str(path))
    iso.close()
    return path