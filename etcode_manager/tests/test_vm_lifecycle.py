"""Tests for cloud-init seed generation and full VM lifecycle (needs QEMU)."""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from etcode_manager.config import Settings
from etcode_manager.qmp import QMPClient, QMPError
from etcode_manager.seed import write_seed_iso, render_user_data
from etcode_manager.vm import VMManager, VMError
from etcode_manager import rfb


class SeedTest(unittest.TestCase):
    def test_user_data_contains_user_and_keys(self):
        data = render_user_data(("ssh-ed25519 AAAA test@host",))
        self.assertIn("#cloud-config", data)
        self.assertIn("name: agent", data)
        self.assertIn("ssh-ed25519 AAAA test@host", data)
        self.assertIn("resize_rootfs: true", data)

    def test_user_data_without_keys(self):
        data = render_user_data(())
        self.assertNotIn("ssh_authorized_keys", data)

    def test_seed_iso_roundtrip(self):
        import pycdlib

        with tempfile.TemporaryDirectory() as tmp:
            iso_path = Path(tmp) / "seed.iso"
            write_seed_iso(iso_path, "inst-1234", "etcode-inst", ("ssh-rsa K",))

            iso = pycdlib.PyCdlib()
            iso.open(str(iso_path))
            out = {}
            for name, iso_path in (("user-data", "/USERDATA.;1"), ("meta-data", "/METADATA.;1")):
                extract = Path(tmp) / name
                iso.get_file_from_iso(str(extract), iso_path=iso_path)
                out[name] = extract.read_text()
            iso.close()

            self.assertIn("inst-1234", out["meta-data"])
            self.assertIn("etcode-inst", out["meta-data"])
            self.assertIn("name: agent", out["user-data"])


def _qemu_available() -> bool:
    return shutil.which("qemu-system-x86_64") and shutil.which("qemu-img")


@unittest.skipUnless(_qemu_available(), "QEMU not installed")
class VMLifecycleTest(unittest.TestCase):
    """End-to-end: create -> start -> QMP + VNC -> stop -> delete."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="etcode-test-")
        base = Path(cls.tmp) / "base.qcow2"
        import subprocess
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(base), "64M"],
                       check=True, capture_output=True)
        cls.settings = Settings(
            base_image=base,
            vms_dir=Path(cls.tmp) / "vms",
            qemu_system=Path(shutil.which("qemu-system-x86_64")),
            qemu_img=Path(shutil.which("qemu-img")),
        )
        cls.manager = VMManager(cls.settings)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_lifecycle(self):
        # -- create
        vm = self.manager.create("test vm 1", cpus=1, memory_mb=256, disk_gb=1)
        vm_dir = self.manager.vm_dir(vm.id)
        self.assertTrue((vm_dir / "disk.qcow2").exists())
        self.assertTrue((vm_dir / "seed.iso").exists())
        self.assertTrue((vm_dir / "vm.json").exists())

        # duplicate name rejected
        with self.assertRaises(VMError):
            self.manager.create("test vm 1")

        # -- start
        vm = self.manager.start(vm)
        self.assertTrue(self.manager.is_running(vm))
        try:
            # QMP answers
            client = QMPClient(port=vm.qmp_port)
            client.connect(timeout=5)
            self.assertEqual(client.query_status(), "running")
            client.close()

            # VNC screen: connect with the real RFB client and get pixels
            frames, connected = [], {}
            c = rfb.RFBClient(
                "127.0.0.1", vm.vnc_port,
                on_frame=lambda *a: frames.append(a),
                on_connected=lambda w, h, n: connected.update(size=(w, h)),
            )
            c.connect(timeout=10)
            deadline = time.time() + 20
            while not frames and time.time() < deadline:
                time.sleep(0.1)
            self.assertTrue(frames, "no screen content from guest")
            rects, w, h, fb = frames[0]
            self.assertEqual((w, h), connected["size"])
            self.assertGreater(len(fb), 0)

            # mouse + keyboard input goes through without error
            c.send_key(0x61, True)
            c.send_key(0x61, False)
            c.send_pointer(0x01, 5, 5)
            c.send_pointer(0x00, 5, 5)
            c.close()

            # -- stop (graceful ACPI via QMP)
            self.manager.stop(vm)
        finally:
            if self.manager.is_running(vm):
                self.manager.stop(vm, force=True)

        self.assertFalse(self.manager.is_running(vm))
        vm = self.manager.list()[0]
        self.assertEqual(vm.status, "stopped")

        # -- delete
        self.manager.delete(vm)
        self.assertEqual(self.manager.list(), [])


if __name__ == "__main__":
    unittest.main()