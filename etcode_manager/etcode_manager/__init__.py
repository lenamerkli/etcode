"""etcode-manager — desktop manager for etcode agent VMs.

Creates per-VM QCOW2 overlays from the immutable etcode base image,
boots them with QEMU (KVM / WHPX / TCG), and provides a desktop app
with VM lifecycle management and an embedded remote screen
(mouse + keyboard) via QEMU's built-in VNC server.
"""

__version__ = "0.1.0"