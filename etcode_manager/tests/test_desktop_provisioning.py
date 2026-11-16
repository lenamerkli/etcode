"""The settings that the manager delivers to the etcode desktop app in the VM, and the image files that install it."""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etcode_manager import config as manager_config
from etcode_manager.config import (
    DEFAULT_BACKEND_URL, Settings, normalize_backend_token, normalize_backend_url,
)
from etcode_manager.seed import (
    DESKTOP_CONFIG_PATH, render_desktop_json, render_user_data, render_write_files, write_seed_iso,
)
from etcode_manager.vm import VM, VMError, VMManager

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

MANAGER = Path(__file__).resolve().parents[1]
DESKTOP = MANAGER.parent / "etcode_desktop"
# Values that would break a naive template: quotes, backslashes, YAML and JSON syntax, braces, non-ASCII.
NASTY_TOKEN = 'a"b\\c\'d#e: {f} [g] - h | > & * ! % @ `i\u00e9\u4e2d'
POSIX_ONLY = unittest.skipIf(sys.platform == "win32", "file modes")


class BackendSettingsTest(unittest.TestCase):
    def test_url_defaults_and_cleaning(self):
        self.assertEqual(normalize_backend_url(""), DEFAULT_BACKEND_URL)
        self.assertEqual(normalize_backend_url("  "), DEFAULT_BACKEND_URL)
        self.assertEqual(normalize_backend_url(" https://example.org:8443/api/ "), "https://example.org:8443/api")
        self.assertEqual(DEFAULT_BACKEND_URL, "http://10.0.2.2:8765")

    def test_bad_urls_are_refused(self):
        for bad in ("10.0.2.2:8765", "ftp://host", "http://", "http://u:p@host", "http://host?x=1",
                    "http://host#f", "http://host:99999", 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_backend_url(bad)

    def test_token_rules(self):
        self.assertEqual(normalize_backend_token(""), "")
        self.assertEqual(normalize_backend_token("  tok-123\n"), "tok-123")
        for bad in ("a b", "a\tb", "caf\u00e9", "a\x00b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_backend_token(bad)

    def test_same_rules_as_the_desktop_app(self):
        """A value that the manager accepts must be accepted by the desktop app, and the other way round."""
        sys.path.insert(0, str(DESKTOP))
        try:
            from etcode_desktop.config import normalize_token, normalize_url
        finally:
            sys.path.remove(str(DESKTOP))
        for url in ("http://10.0.2.2:8765", "https://x.org/api/", "ftp://x", "http://", "http://u:p@h", "http://h?q", "nope"):
            with self.subTest(url=url):
                try:
                    expected = normalize_url(url)
                except ValueError:
                    with self.assertRaises(ValueError):
                        normalize_backend_url(url)
                else:
                    self.assertEqual(normalize_backend_url(url), expected)
        for token in ("abc", " abc \n", "a b", "caf\u00e9", "a\x01"):
            with self.subTest(token=token):
                try:
                    expected = normalize_token(token) or ""
                except ValueError:
                    with self.assertRaises(ValueError):
                        normalize_backend_token(token)
                else:
                    self.assertEqual(normalize_backend_token(token), expected)

    def test_load_settings_reads_backend_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"backend_url": "https://b.example/", "backend_token": " tok ", "other": 1}))
            with patch.object(manager_config, "CONFIG_FILE", cfg):
                settings = manager_config.load_settings()
        self.assertEqual(settings.backend_url, "https://b.example")
        self.assertEqual(settings.backend_token, "tok")
        self.assertEqual(settings.extra, {"other": 1})

    def test_load_settings_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager_config, "CONFIG_FILE", Path(tmp) / "missing.json"):
                settings = manager_config.load_settings()
        self.assertEqual((settings.backend_url, settings.backend_token), (DEFAULT_BACKEND_URL, ""))

    def test_invalid_config_is_reported_without_the_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"backend_url": "ftp://x", "backend_token": "sekrit token"}))
            with patch.object(manager_config, "CONFIG_FILE", cfg), self.assertRaises(RuntimeError) as caught:
                manager_config.load_settings()
        self.assertNotIn("sekrit", str(caught.exception))

    def test_token_is_not_in_repr(self):
        self.assertNotIn("sekrit", repr(Settings(backend_token="sekrit")))
        self.assertNotIn("sekrit", repr(VM(id="i", name="n", backend_token="sekrit")))


class SeedWriteFilesTest(unittest.TestCase):
    def test_json_has_the_keys_the_desktop_reads(self):
        self.assertEqual(json.loads(render_desktop_json("http://h:1", "tok")), {"backend_url": "http://h:1", "token": "tok"})
        self.assertEqual(json.loads(render_desktop_json("http://h:1", "")), {"backend_url": "http://h:1"})
        self.assertEqual(json.loads(render_desktop_json("", "tok")), {"token": "tok"})
        self.assertEqual(render_desktop_json("", ""), "")

    def test_no_section_without_values(self):
        self.assertEqual(render_write_files(), "")
        self.assertNotIn("write_files", render_user_data(()))

    def test_section_shape(self):
        section = render_write_files("http://h:1", "tok")
        self.assertIn(f"path: {DESKTOP_CONFIG_PATH}", section)
        self.assertEqual(DESKTOP_CONFIG_PATH, "/etc/etcode/desktop.json")
        self.assertIn("owner: agent:agent", section)
        self.assertIn('permissions: "0600"', section)
        self.assertIn("defer: true", section)

    def test_ssh_keys_still_rendered(self):
        data = render_user_data(("ssh-ed25519 AAAA test@host",), "http://h:1", "tok")
        self.assertIn('"ssh-ed25519 AAAA test@host"', data)
        self.assertIn("name: agent", data)
        self.assertIn("resize_rootfs: true", data)

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_user_data_is_valid_yaml_for_any_token(self):
        for token in ("simple", NASTY_TOKEN, "{{x}}", "{0}", "%s", "\\n", "'", '"', "}"):
            with self.subTest(token=token):
                data = render_user_data(("ssh-ed25519 AAAA a@b",), "http://h:1/x", token)
                doc = yaml.safe_load(data)
                self.assertEqual(doc["users"][0]["name"], "agent")
                self.assertEqual(doc["users"][0]["ssh_authorized_keys"], ["ssh-ed25519 AAAA a@b"])
                self.assertTrue(doc["resize_rootfs"])
                (entry,) = doc["write_files"]
                self.assertEqual(entry["path"], "/etc/etcode/desktop.json")
                self.assertEqual(entry["owner"], "agent:agent")
                self.assertEqual(entry["permissions"], "0600")
                self.assertIs(entry["defer"], True)
                # what cloud-init writes is exactly what the desktop app parses
                self.assertEqual(json.loads(entry["content"]), {"backend_url": "http://h:1/x", "token": token})
                self.assertEqual(entry["content"].count("\n"), 1)

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_odd_ssh_key_stays_valid_yaml(self):
        data = render_user_data(('ssh-rsa A"B\\C comment: x',))
        self.assertEqual(yaml.safe_load(data)["users"][0]["ssh_authorized_keys"], ['ssh-rsa A"B\\C comment: x'])

    @POSIX_ONLY
    def test_seed_iso_is_private_and_carries_the_settings(self):
        import pycdlib

        with tempfile.TemporaryDirectory() as tmp:
            iso_path = Path(tmp) / "seed.iso"
            iso_path.write_bytes(b"old")
            os.chmod(iso_path, 0o644)  # an existing, readable seed must become private as well
            write_seed_iso(iso_path, "inst-1", "etcode-inst", (), "http://10.0.2.2:8765", "tok-xyz")
            self.assertEqual(stat.S_IMODE(iso_path.stat().st_mode), 0o600)
            iso = pycdlib.PyCdlib()
            iso.open(str(iso_path))
            extract = Path(tmp) / "user-data"
            iso.get_file_from_iso(str(extract), iso_path="/USERDATA.;1")
            iso.close()
            text = extract.read_text()
        self.assertIn("/etc/etcode/desktop.json", text)
        self.assertIn("tok-xyz", text)


def make_manager(tmp: str, **settings_kwargs) -> VMManager:
    base = Path(tmp) / "base.qcow2"
    base.touch()
    settings = Settings(
        base_image=base, vms_dir=Path(tmp) / "vms",
        qemu_system=Path("qemu-system-x86_64"), qemu_img=Path("qemu-img"), **settings_kwargs,
    )
    return VMManager(settings)


def fake_run(argv):
    """Stands in for qemu-img: the overlay only has to exist."""
    if argv[1] == "create":
        Path(argv[-1]).touch()


def create(manager: VMManager, name="vm-1", **kwargs) -> VM:
    with patch.object(VMManager, "_run", staticmethod(fake_run)):
        return manager.create(name, **kwargs)


class CreateVMTest(unittest.TestCase):
    def read_user_data(self, manager: VMManager, vm: VM) -> str:
        import pycdlib
        iso = pycdlib.PyCdlib()
        iso.open(str(manager.vm_dir(vm.id) / "seed.iso"))
        target = manager.vm_dir(vm.id) / "user-data.txt"
        iso.get_file_from_iso(str(target), iso_path="/USERDATA.;1")
        iso.close()
        return target.read_text()

    def test_defaults_come_from_the_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = make_manager(tmp, backend_url="http://10.0.2.2:9000", backend_token="from-config")
            vm = create(manager)
            self.assertEqual((vm.backend_url, vm.backend_token), ("http://10.0.2.2:9000", "from-config"))
            stored = json.loads((manager.vm_dir(vm.id) / "vm.json").read_text())
            self.assertEqual(stored["backend_url"], "http://10.0.2.2:9000")
            self.assertEqual(stored["backend_token"], "from-config")
            self.assertIn("from-config", self.read_user_data(manager, vm))

    def test_a_vm_can_get_its_own_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = make_manager(tmp, backend_token="shared")
            vm = create(manager, backend_url="https://srv.example", backend_token="own-token")
            data = self.read_user_data(manager, vm)
            self.assertIn("own-token", data)
            self.assertNotIn("shared", data)
            self.assertIn("https://srv.example", data)
            # listing the VMs again (a new manager, as after a restart) brings the values back
            (again,) = VMManager(manager.settings).list()
            self.assertEqual((again.backend_url, again.backend_token), ("https://srv.example", "own-token"))

    def test_empty_values_mean_default_url_and_no_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = make_manager(tmp, backend_token="shared")
            vm = create(manager, backend_url="", backend_token="")
            self.assertEqual((vm.backend_url, vm.backend_token), (DEFAULT_BACKEND_URL, ""))
            data = self.read_user_data(manager, vm)
            self.assertIn(DEFAULT_BACKEND_URL, data)
            self.assertNotIn("token", data)

    def test_bad_values_create_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = make_manager(tmp)
            for kwargs in ({"backend_url": "ftp://x"}, {"backend_token": "has space"}):
                with self.subTest(kwargs=kwargs), self.assertRaises(VMError):
                    create(manager, **kwargs)
            self.assertEqual(list(manager.vms_dir().iterdir()), [])

    @POSIX_ONLY
    def test_vm_json_and_seed_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = make_manager(tmp, backend_token="tok")
            vm = create(manager)
            vm_dir = manager.vm_dir(vm.id)
            for name in ("vm.json", "seed.iso"):
                self.assertEqual(stat.S_IMODE((vm_dir / name).stat().st_mode), 0o600, name)
            # saving again (what start and stop do) keeps it private and leaves no temp file behind
            os.chmod(vm_dir / "vm.json", 0o644)
            manager._save(vm)
            self.assertEqual(stat.S_IMODE((vm_dir / "vm.json").stat().st_mode), 0o600)
            self.assertEqual(sorted(p.name for p in vm_dir.glob("vm.json*")), ["vm.json"])

    def test_old_vm_json_without_backend_fields_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = make_manager(tmp)
            d = manager.vms_dir() / "old"
            d.mkdir()
            (d / "vm.json").write_text(json.dumps({"id": "old", "name": "old", "created": 1.0}))
            (vm,) = manager.list()
            self.assertEqual((vm.backend_url, vm.backend_token), ("", ""))

    def test_seed_can_be_regenerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = make_manager(tmp, backend_token="tok")
            vm = create(manager)
            manager.write_seed(vm)
            self.assertIn("tok", self.read_user_data(manager, vm))


class NewVMDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_prefilled_with_the_defaults_and_hides_the_token(self):
        from PySide6.QtWidgets import QLineEdit
        from etcode_manager.app import NewVMDialog
        dialog = NewVMDialog(backend_url="http://10.0.2.2:9000", backend_token="tok")
        self.assertEqual(dialog.backend_values(), ("http://10.0.2.2:9000", "tok"))
        self.assertEqual(dialog.token_edit.echoMode(), QLineEdit.EchoMode.Password)
        dialog.show_token.setChecked(True)
        self.assertEqual(dialog.token_edit.echoMode(), QLineEdit.EchoMode.Normal)

    def test_defaults_without_arguments(self):
        from etcode_manager.app import NewVMDialog
        self.assertEqual(NewVMDialog().backend_values(), (DEFAULT_BACKEND_URL, ""))

    def test_each_vm_can_get_other_values(self):
        from etcode_manager.app import NewVMDialog
        dialog = NewVMDialog(backend_url="http://10.0.2.2:9000", backend_token="tok")
        dialog.backend_url_edit.setText("https://srv.example/")
        dialog.token_edit.setText(" other \n")
        self.assertEqual(dialog.backend_values(), ("https://srv.example", "other"))

    def test_invalid_values_keep_the_dialog_open(self):
        from PySide6.QtWidgets import QDialog
        from etcode_manager.app import NewVMDialog
        dialog = NewVMDialog()
        dialog.show()
        dialog.backend_url_edit.setText("not a url")
        dialog.accept()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertFalse(dialog.error_label.isHidden())
        self.assertIn("http", dialog.error_label.text())
        dialog.backend_url_edit.setText("http://10.0.2.2:8765")
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_window_passes_the_values_on(self):
        from PySide6.QtWidgets import QDialog
        from etcode_manager import app as app_module
        with tempfile.TemporaryDirectory() as tmp:
            manager = make_manager(tmp, backend_url="http://10.0.2.2:9000", backend_token="tok")
            window = app_module.MainWindow(manager.settings, manager)
            try:
                seen = {}

                class FakeDialog(app_module.NewVMDialog):
                    def __init__(self, parent=None, **kwargs):
                        super().__init__(parent, **kwargs)
                        seen.update(kwargs)
                        self.name_edit.setText("vm-x")
                        self.token_edit.setText("changed")

                    def exec(self):
                        return QDialog.DialogCode.Accepted

                with patch.object(app_module, "NewVMDialog", FakeDialog), \
                        patch.object(VMManager, "_run", staticmethod(fake_run)):
                    window.on_new()
                self.assertEqual(seen, {"backend_url": "http://10.0.2.2:9000", "backend_token": "tok"})
                (vm,) = manager.list()
                self.assertEqual((vm.name, vm.backend_url, vm.backend_token), ("vm-x", "http://10.0.2.2:9000", "changed"))
                window.refresh()
                self.assertIn("token set", window.info.text())
                self.assertNotIn("changed", window.info.text())
            finally:
                window.close()


class ImageFilesTest(unittest.TestCase):
    """The files that install the desktop app into the image fit together (the image itself is built by build_image.sh)."""

    def setUp(self):
        self.build = (MANAGER / "build_image.sh").read_text()
        self.provision = (MANAGER / "provision" / "provision.sh").read_text()

    def test_scripts_parse(self):
        for script in (MANAGER / "build_image.sh", MANAGER / "provision" / "provision.sh"):
            with self.subTest(script=script.name):
                result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_packaging_files_exist_and_are_staged(self):
        packaging = DESKTOP / "packaging"
        for name in ("etcode-desktop", "etcode-desktop.desktop", "etcode-desktop-autostart.desktop", "etcode-desktop.svg"):
            self.assertTrue((packaging / name).is_file(), name)
            self.assertIn(name, self.build)
        self.assertIn("DESKTOP_DIR", self.build)

    def test_qt_libraries_are_in_the_package_list(self):
        packages = re.search(r"GUEST_PACKAGES=\((.*?)\n\)", self.build, re.S).group(1)
        packages = re.sub(r"#.*", "", packages).split()
        for name in ("libegl1", "libgl1", "libxkbcommon-x11-0", "libxcb-cursor0", "libxcb-icccm4", "libxcb-image0",
                     "libxcb-keysyms1", "libxcb-randr0", "libxcb-render-util0", "libxcb-shape0", "libxcb-util1",
                     "libxcb-xkb1", "libxcb-xinerama0"):
            self.assertIn(name, packages)

    def test_provision_installs_what_is_staged(self):
        staged = self.build.replace("$STAGING_DIR/", "")
        for target in ("/usr/local/bin/etcode-desktop", "/usr/share/applications/etcode-desktop.desktop",
                       "/etc/xdg/autostart/etcode-desktop.desktop",
                       "/usr/share/icons/hicolor/scalable/apps/etcode-desktop.svg"):
            self.assertIn(target, self.provision)
            self.assertIn(target.lstrip("/"), staged)
        self.assertIn("PySide6-Essentials", self.provision)
        self.assertIn("/opt/etcode/.venv", self.provision)
        self.assertIn("etcode-desktop", re.search(r"for desktop_file in ([^;]*); do", self.provision).group(1))

    def test_no_token_is_baked_into_the_image(self):
        for text in (self.build, self.provision):
            self.assertNotIn("backend_token", text)
            self.assertNotIn("ETCODE_API_TOKEN", text)

    def test_panel_has_an_etcode_launcher(self):
        root = ET.parse(MANAGER / "provision" / "xfce4-panel.xml").getroot()
        panel = next(p for p in root.iter("property") if p.get("name") == "panel-1")
        ids = [int(v.get("value")) for v in next(p for p in panel if p.get("name") == "plugin-ids")]
        self.assertEqual(len(ids), len(set(ids)))
        plugins = {p.get("name"): p for p in next(p for p in root if p.get("name") == "plugins")}
        for plugin_id in ids:
            self.assertIn(f"plugin-{plugin_id}", plugins)
        # the quick-launch group stays before the separator, and the new one is the last of it
        self.assertLess(ids.index(11), ids.index(10))
        self.assertEqual(ids[ids.index(11) - 1], 5)
        launcher = plugins["plugin-11"]
        self.assertEqual(launcher.get("value"), "launcher")
        (items,) = [p for p in launcher if p.get("name") == "items"]
        self.assertEqual([v.get("value") for v in items], ["17929499125.desktop"])
        self.assertIn("launcher-11/17929499125.desktop", self.build)
        # every other launcher plugin has its item directory in provision/panel
        for name, plugin in plugins.items():
            if plugin.get("value") == "launcher" and name != "plugin-11":
                number = name.split("-")[1]
                self.assertTrue((MANAGER / "provision" / "panel" / f"launcher-{number}").is_dir(), name)


if __name__ == "__main__":
    unittest.main()
