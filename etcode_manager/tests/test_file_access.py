"""Opt-in SFTP forwarding, stable addresses and the manager's file actions."""

import json
import os
import socket
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pycdlib

from etcode_manager import config
from etcode_manager.config import Settings
from etcode_manager.vm import VM, VMError, VMManager

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The manager checks the shape of the public-key line; no private key is needed for these tests.
PUBLIC_KEY = "ssh-ed25519 AAAA some@host"


class FileAccessTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        tmp = Path(directory.name)
        base = tmp / "base.qcow2"
        base.touch()
        self.settings = Settings(
            base_image=base, vms_dir=tmp / "vms",
            qemu_system=Path("qemu-system-x86_64"), qemu_img=Path("qemu-img"),
        )
        self.manager = VMManager(self.settings)

    def create(self, name="test VM", **kwargs):
        def fake_qemu_img(argv):
            if argv[1] == "create":
                Path(argv[-1]).touch()

        with patch.object(self.manager, "_run", side_effect=fake_qemu_img):
            return self.manager.create(name, **kwargs)

    def test_new_vm_opt_in_requires_a_public_key_and_seeds_it(self):
        with self.assertRaisesRegex(VMError, "SSH public key"):
            self.create(file_access=True)
        self.assertEqual(self.manager.list(), [])

        self.settings.ssh_public_key = PUBLIC_KEY
        vm = self.create(file_access=True)
        self.assertGreater(vm.ssh_port, 0)
        self.assertEqual(vm.sftp_url, f"sftp://agent@127.0.0.1:{vm.ssh_port}/home/agent/")
        seed = self.manager.vm_dir(vm.id) / "seed.iso"
        iso = pycdlib.PyCdlib()
        iso.open(str(seed))
        try:
            data = BytesIO()
            iso.get_file_from_iso_fp(data, iso_path="/USERDATA.;1")
        finally:
            iso.close()
        self.assertIn(PUBLIC_KEY, data.getvalue().decode())
        self.assertIn(b"ssh_pwauth: false", data.getvalue())

    def test_old_metadata_is_disabled_and_enable_does_not_replace_the_seed(self):
        vm = self.create()
        vm_dir = self.manager.vm_dir(vm.id)
        metadata = vm_dir / "vm.json"
        legacy_data = json.loads(metadata.read_text())
        del legacy_data["ssh_port"]
        metadata.write_text(json.dumps(legacy_data))
        (vm,) = self.manager.list()
        self.assertIsNone(vm.sftp_url)

        seed_before = (vm_dir / "seed.iso").read_bytes()
        self.manager.set_file_access(vm, True)  # An existing guest can already have its SSH key.
        self.assertEqual((vm_dir / "seed.iso").read_bytes(), seed_before)
        (reopened,) = VMManager(self.settings).list()
        self.assertEqual(reopened.sftp_url, vm.sftp_url)
        self.manager.set_file_access(reopened, False)
        self.assertIsNone(self.manager.list()[0].sftp_url)

    def test_stopped_vms_keep_distinct_ports(self):
        self.settings.ssh_public_key = PUBLIC_KEY
        a = self.create("a", file_access=True)
        b = self.create("b", file_access=True)
        self.assertNotEqual(a.ssh_port, b.ssh_port)
        self.manager.set_file_access(a, True)
        ports = {vm.name: vm.ssh_port for vm in VMManager(self.settings).list()}
        self.assertEqual(ports, {"a": a.ssh_port, "b": b.ssh_port})

    def test_cannot_toggle_a_running_vm(self):
        vm = self.create()
        with patch.object(self.manager, "is_running", return_value=True), \
                self.assertRaisesRegex(VMError, "Stop the VM"):
            self.manager.set_file_access(vm, True)
        self.assertIsNone(self.manager.list()[0].sftp_url)

    def test_reserved_conflict_fails_without_changing_the_bookmark(self):
        vm = self.create()
        self.manager.set_file_access(vm, True)
        url = vm.sftp_url
        with socket.socket() as listener, patch("etcode_manager.vm.subprocess.Popen") as popen:
            listener.bind(("127.0.0.1", vm.ssh_port))
            listener.listen()
            with self.assertRaisesRegex(VMError, f"{vm.ssh_port} is unavailable"):
                self.manager.start(vm)
            popen.assert_not_called()
        self.assertEqual(self.manager.list()[0].sftp_url, url)

    def test_enabled_forward_binds_only_ipv4_loopback(self):
        vm = self.create()
        self.manager.set_file_access(vm, True)
        with patch("etcode_manager.vm.QMPClient"), patch("etcode_manager.vm.subprocess.Popen") as popen:
            popen.return_value.pid = 12345
            popen.return_value.poll.return_value = None
            self.manager.start(vm)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("-netdev") + 1], f"user,id=net0,hostfwd=tcp:127.0.0.1:{vm.ssh_port}-:22")

    def test_malformed_port_is_not_a_qemu_option(self):
        for value in (True, -1, 22, 65536, "22000-:22,hostfwd=tcp::9000-:22"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "SSH port"):
                VM(id="test", name="vm", ssh_port=value)


class FileAccessGUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from etcode_manager.app import MainWindow

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.settings = Settings(vms_dir=self.root / "vms")
        self.manager = VMManager(self.settings)
        self.vm = VM(id="id", name="test VM")
        self.manager.vm_dir(self.vm.id).mkdir()
        self.manager._save(self.vm)
        self.window = MainWindow(self.settings, self.manager)
        self.addCleanup(self.window.close)

    def test_toolbar_toggle_and_copy_follow_vm_state(self):
        w = self.window
        self.assertTrue(w.act_file_access.isEnabled())
        self.assertFalse(w.act_file_access.isChecked())
        self.assertFalse(w.act_open_files.isEnabled())
        self.assertFalse(w.act_copy_sftp.isEnabled())
        w.act_file_access.trigger()
        vm = self.manager.list()[0]
        self.assertTrue(w.act_file_access.isChecked())
        self.assertIsNotNone(vm.sftp_url)
        self.assertFalse(w.act_open_files.isEnabled())
        with patch.object(self.manager, "is_running", return_value=True):
            w.refresh()
            self.assertFalse(w.act_file_access.isEnabled())
            self.assertTrue(w.act_open_files.isEnabled())
            self.assertTrue(w.act_copy_sftp.isEnabled())
            w.act_copy_sftp.trigger()
            self.assertEqual(self.app.clipboard().text(), vm.sftp_url)
        w.refresh()
        w.act_file_access.trigger()
        self.assertIsNone(self.manager.list()[0].sftp_url)
        self.assertFalse(w.act_file_access.isChecked())

    def test_opens_the_sftp_url_in_dolphin_not_the_default_handler(self):
        self.manager.set_file_access(self.vm, True)
        with (
            patch.object(self.manager, "is_running", return_value=True),
            patch("etcode_manager.app.sys.platform", "linux"),
            patch("etcode_manager.app.shutil.which", return_value="/usr/bin/dolphin") as which,
            patch("etcode_manager.app.QProcess.startDetached", return_value=(True, 42)) as start,
            patch("etcode_manager.app.QDesktopServices.openUrl") as handler,
        ):
            self.window.refresh()
            self.window.act_open_files.trigger()
        which.assert_called_once_with("dolphin")
        start.assert_called_once_with("/usr/bin/dolphin", [self.vm.sftp_url])
        handler.assert_not_called()

    def test_missing_dolphin_and_no_handler_points_to_copy(self):
        self.manager.set_file_access(self.vm, True)
        with (
            patch.object(self.manager, "is_running", return_value=True),
            patch("etcode_manager.app.shutil.which", return_value=None),
            patch("etcode_manager.app.QDesktopServices.openUrl", return_value=False) as handler,
            patch("etcode_manager.app.QMessageBox.information") as message,
        ):
            self.window.refresh()
            self.window.act_open_files.trigger()
        self.assertEqual(handler.call_args.args[0].toString(), self.vm.sftp_url)
        self.assertIn("Copy SFTP Address", message.call_args.args[2])

    def test_settings_save_only_the_public_key_for_future_vms(self):
        from PySide6.QtWidgets import QDialog
        from etcode_manager.app import SettingsDialog

        class EditedDialog(SettingsDialog):
            def exec(self):
                self.ssh_key_edit.setText(f" {PUBLIC_KEY} ")
                self.accept()
                return self.result()

        local = self.root / "config.local.json"
        tracked = self.root / "config.json"
        seed_before = self.manager.vm_dir(self.vm.id) / "seed.iso"
        self.assertFalse(seed_before.exists())
        with (
            patch.multiple(config, CONFIG_FILE=tracked, LOCAL_CONFIG_FILE=local),
            patch("etcode_manager.app.SettingsDialog", EditedDialog),
        ):
            self.window.on_settings()
            self.assertEqual(config.load_settings().ssh_public_key, PUBLIC_KEY)
        self.assertFalse(seed_before.exists())
        self.assertFalse(tracked.exists())

        invalid = SettingsDialog()
        invalid.ssh_key_edit.setText("-----BEGIN OPENSSH PRIVATE KEY-----")
        invalid.accept()
        self.assertNotEqual(invalid.result(), QDialog.DialogCode.Accepted)
        self.assertIn(".pub", invalid.error_label.text())


if __name__ == "__main__":
    unittest.main()
