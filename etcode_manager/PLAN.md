For `etcode-manager`, I’d use an **immutable Debian template image**, with a **writable QCOW2 overlay for each VM**. The overlay stores only that VM’s changes, so you don’t have to duplicate the whole environment for every agent. QEMU supports this through backing files. 

```text
etcode-base-v1.qcow2      ← OS, tools, agent runtime; never modified
  ├── task-001.qcow2     ← changes made by one VM
  └── task-002.qcow2     ← changes made by another VM
```

Separate **building the template**—which can happen in Linux CI—from **creating instances** on Windows, Fedora, and Debian.

## 1. Build the custom Debian image programmatically

Start with an official Debian **generic cloud image**. It already has cloud-init support; Debian recommends the `generic` variant for maximum device compatibility. Don’t confuse Debian’s `nocloud` image variant with cloud-init’s *NoCloud* configuration mechanism: the former does not run cloud-init. 

For example, on a Debian build machine:

```bash
set -eu

sudo apt-get update
sudo apt-get install -y qemu-utils guestfs-tools curl

curl -fL \
  https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
  -o upstream.qcow2

qemu-img convert -f qcow2 -O qcow2 upstream.qcow2 build.qcow2

virt-customize --format qcow2 -a build.qcow2 \
  --install cloud-init,cloud-guest-utils,openssh-server,ca-certificates,git,python3 \
  --run ./provision.sh \
  --run-command 'cloud-init clean --logs --machine-id --seed' \
  --run-command 'rm -f /etc/ssh/ssh_host_*; apt-get clean'

qemu-img convert -f qcow2 -O qcow2 \
  build.qcow2 etcode-base-v1-amd64.qcow2
```

`provision.sh` is your script for installing the etcode runtime, task tools, configuration, and optionally a systemd service. `virt-customize` installs packages and runs scripts against the image without normally booting the guest; scripts run inside a chroot in a libguestfs appliance. 

The cleanup is important: it removes cloud-init state and resets the machine ID for cloning. Removing SSH host keys lets cloud-init generate fresh ones for each instance. **Don’t bake API tokens, private keys, or personal workspaces into the template.** 

For released builds, pin the upstream image and its checksum instead of relying on the moving `latest` URL.

## 2. Give each VM its own disk and first-boot configuration

Use **cloud-init’s NoCloud datasource** to supply the instance’s hostname, SSH access, and other settings. It reads `user-data` and `meta-data` from an ISO or FAT filesystem labeled `CIDATA`. Each new VM should get a unique `instance-id`; retain that ID on subsequent boots of the same VM. 

Create a `user-data` file:

```yaml
#cloud-config
users:
  - name: agent
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - "REPLACE_WITH_YOUR_MANAGER_SSH_PUBLIC_KEY"

ssh_pwauth: false
disable_root: true

growpart:
  mode: auto
  devices: ["/"]
resize_rootfs: true
```

The last settings grow the root partition and filesystem when the VM’s virtual disk is enlarged. `qemu-img resize` alone only enlarges the virtual disk—not its filesystem. 

Here is a Python example for creating the disk and configuration ISO. It uses `qemu-img` from your host’s QEMU installation and `pycdlib` to generate the ISO. 

```bash
python -m pip install pycdlib
```

```python
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import subprocess

import pycdlib

base = Path("etcode-base-v1-amd64.qcow2").resolve(strict=True)
instance_id = str(uuid4())

vm_dir = (Path("vms") / instance_id).resolve()
vm_dir.mkdir(parents=True)

disk = vm_dir / "disk.qcow2"
seed = vm_dir / "seed.iso"

# A writable clone backed by the immutable template.
subprocess.run([
    "qemu-img", "create",
    "-f", "qcow2", "-F", "qcow2",
    "-b", str(base), str(disk),
], check=True)

subprocess.run([
    "qemu-img", "resize", "-f", "qcow2", str(disk), "20G",
], check=True)

files = {
    "user-data": Path("user-data").read_bytes(),
    "meta-data": (
        f"instance-id: {instance_id}\n"
        f"local-hostname: etcode-{instance_id[:8]}\n"
    ).encode("utf-8"),
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

iso.write(str(seed))
iso.close()

print(f"VM directory: {vm_dir}")
```

Boot QEMU with `disk.qcow2` as the writable system disk and `seed.iso` attached read-only. For example, these drive arguments:

```text
-drive file=.../disk.qcow2,format=qcow2,if=virtio
-drive file=.../seed.iso,format=raw,if=virtio,readonly=on
```

Cloud-init documents this approach specifically for QEMU-backed instances. 

For matching x86-64 hosts and guests, use **KVM on Fedora/Debian**, **WHPX on Windows**, or **TCG as an emulation fallback**, depending on availability. These are runtime choices, not reasons to rebuild the Debian environment separately for each host OS. 

## 3. When you need an actual independent copy

An overlay **depends on its base image**. To export a VM as a standalone, copyable file, shut it down and flatten its disk:

```bash
qemu-img convert -f qcow2 -O qcow2 \
  vms/INSTANCE_ID/disk.qcow2 \
  exported-vm.qcow2
```

Without an output backing file, this creates an independent image containing the VM’s complete disk state. Never perform this against a disk being modified by a running VM. 

Two important management rules:

- **Version your templates:** publish `base-v2.qcow2` rather than modifying `base-v1.qcow2` underneath existing overlays.
- **Keep base images at stable paths:** the example records an absolute backing-file path, so an overlay alone is not a portable export. 

**My starting stack would be:** Debian generic cloud image → `virt-customize` build script → versioned QCOW2 template → per-VM overlay and NoCloud seed. If provisioning requires a fully booted system rather than a chroot, use **Packer’s QEMU builder** for the image-building stage instead. 