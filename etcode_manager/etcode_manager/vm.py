"""VM lifecycle management: create, start, stop and delete etcode VMs.

Layout per VM (see PLAN.md):

    vms/<instance-id>/
        disk.qcow2   — writable overlay backed by the immutable base image
        seed.iso     — cloud-init NoCloud CIDATA seed
        vm.json      — metadata (name, resources, instance-id, ports, backend URL and token; mode 0600)
        qemu.log     — guest console / QEMU stderr

VM state is discovered by scanning the vms directory, so it survives
application restarts.  QEMU is launched detached with a QMP control socket
and a VNC display on loopback. VMs with file access enabled also have a stable
loopback port forwarded to the guest's SSH server.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, asdict, field, replace
from pathlib import Path

from .config import Settings, normalize_backend_token, normalize_backend_url, normalize_ssh_public_key
from .qmp import QMPClient, QMPError
from .seed import write_seed_iso

VM_JSON = "vm.json"
SFTP_HOST = "127.0.0.1"
# Below the default ephemeral port range and separate from the VNC displays.
SFTP_PORTS = range(22000, 23000)


class VMError(RuntimeError):
    pass


@dataclass
class VM:
    """Persistent metadata of one VM."""

    id: str                  # instance id (kept across boots)
    name: str
    cpus: int = 2
    memory_mb: int = 2048
    disk_gb: int = 20
    created: float = 0.0
    vnc_port: int = 0        # runtime (re-assigned on each start)
    qmp_port: int = 0
    pid: int = 0             # QEMU PID while running (survives app restarts)
    status: str = "stopped"  # runtime: stopped | running
    # The desktop app inside the VM gets these from the cloud-init seed. They are kept here because the seed
    # needs them whenever it is regenerated. The token is a secret: vm.json is private, and it stays out of repr().
    backend_url: str = ""
    backend_token: str = field(default="", repr=False)
    ssh_port: int = 0       # persistent; 0 means no forward (including in older vm.json files)

    def __post_init__(self) -> None:
        # This value goes into a comma-separated QEMU option and must be a literal port number.
        if type(self.ssh_port) is not int or not (self.ssh_port == 0 or 1024 <= self.ssh_port <= 65535):
            raise ValueError("Invalid SSH port in VM metadata.")

    @property
    def sftp_url(self) -> str | None:
        if self.ssh_port:
            return f"sftp://agent@{SFTP_HOST}:{self.ssh_port}/home/agent/"
        return None

    @property
    def hostname(self) -> str:
        return f"etcode-{self.id[:8]}"


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists (POSIX and Windows)."""
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION, 0, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bind_sftp_port(port: int) -> socket.socket:
    """Probe/reserve a loopback TCP port until QEMU is ready to take it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            # Like QEMU's user networking, allow a restart while old connections are in TIME_WAIT.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((SFTP_HOST, port))
    except OSError:
        sock.close()
        raise
    return sock


def _free_vnc_display() -> tuple[int, int]:
    """Return ``(display, port)`` for a free VNC display (port = 5900+display)."""
    for display in range(100):
        port = 5900 + display
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return display, port
    raise VMError("No free VNC display (ports 5900-5999 all busy).")


class VMManager:
    """Creates and controls VMs in a directory tree."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._procs: dict[str, subprocess.Popen] = {}

    # -- registry -----------------------------------------------------------

    def vms_dir(self) -> Path:
        d = self.settings.vms_dir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def vm_dir(self, vm_id: str) -> Path:
        return self.vms_dir() / vm_id

    def list(self) -> list[VM]:
        """All VMs known in the vms directory, refreshed runtime status."""
        vms = []
        for meta in sorted(self.vms_dir().glob(f"*/{VM_JSON}")):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                vm = VM(**{k: v for k, v in data.items() if k in VM.__dataclass_fields__})
            except (OSError, ValueError, TypeError):
                continue
            vm.status = "running" if self.is_running(vm) else "stopped"
            vms.append(vm)
        vms.sort(key=lambda v: v.created)
        return vms

    def _save(self, vm: VM) -> None:
        """Write vm.json. It may hold the backend token, so it is created private (0600) and replaced atomically."""
        path = self.vm_dir(vm.id) / VM_JSON
        tmp = path.with_name(VM_JSON + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(asdict(vm), indent=2))
            try:
                os.chmod(tmp, 0o600)  # a leftover temp file keeps its old mode otherwise
            except OSError:
                pass  # not every file system has modes (Windows)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- file access --------------------------------------------------------

    def _free_sftp_port(self) -> int:
        # A stopped VM still owns its assigned port: its Dolphin bookmark must not point at a different VM.
        reserved = {vm.ssh_port for vm in self.list()}
        for port in SFTP_PORTS:
            if port not in reserved:
                try:
                    with _bind_sftp_port(port):
                        return port
                except OSError:
                    continue
        raise VMError(f"No free loopback port for file access ({SFTP_PORTS.start}–{SFTP_PORTS.stop - 1}).")

    def set_file_access(self, vm: VM, enabled: bool) -> None:
        """Change a stopped VM's forwarding on the next boot; leave its disk and seed alone."""
        if self.is_running(vm):
            raise VMError("Stop the VM before changing file access.")
        if enabled == bool(vm.ssh_port):
            return
        port = self._free_sftp_port() if enabled else 0
        try:
            self._save(replace(vm, ssh_port=port))
        except OSError as exc:
            raise VMError(f"Could not save file access settings: {exc}") from exc
        vm.ssh_port = port

    # -- create -------------------------------------------------------------

    def create(
        self,
        name: str,
        cpus: int = 2,
        memory_mb: int = 2048,
        disk_gb: int = 20,
        backend_url: str | None = None,
        backend_token: str | None = None,
        file_access: bool = False,
    ) -> VM:
        """Create a new VM: overlay disk + cloud-init seed ISO.

        ``backend_url`` and ``backend_token`` are what the desktop app inside the VM will use. ``None`` means the
        defaults from the settings (``config.local.json``); an empty URL means the default URL, an empty token means none.
        ``file_access`` opts into the SSH forward; the host's public key must be configured.
        """
        self.settings.require_tools()
        name = name.strip()
        if not _NAME_RE.match(name):
            raise VMError(
                "Invalid name: use letters, digits, spaces, '.', '_' or '-' (max 64 chars)."
            )
        try:
            backend_url = normalize_backend_url(self.settings.backend_url if backend_url is None else backend_url)
            backend_token = normalize_backend_token(
                self.settings.backend_token if backend_token is None else backend_token
            )
            ssh_public_key = normalize_ssh_public_key(self.settings.ssh_public_key)
        except ValueError as exc:
            raise VMError(str(exc)) from exc
        if file_access and not ssh_public_key:
            raise VMError("Set an SSH public key in Settings before enabling file access for a new VM.")
        if any(vm.name == name for vm in self.list()):
            raise VMError(f"A VM named '{name}' already exists.")
        if not self.settings.base_image.exists():
            raise VMError(
                f"Base image not found: {self.settings.base_image}. "
                "Run build_image.sh first or set 'base_image' in config.json."
            )

        vm = VM(id=str(uuid.uuid4()), name=name, cpus=cpus, memory_mb=memory_mb,
                disk_gb=disk_gb, created=time.time(), backend_url=backend_url, backend_token=backend_token,
                ssh_port=self._free_sftp_port() if file_access else 0)
        vm_dir = self.vm_dir(vm.id)
        vm_dir.mkdir(parents=True, exist_ok=False)
        disk = vm_dir / "disk.qcow2"
        try:
            # Writable overlay backed by the immutable template.
            self._run([
                str(self.settings.qemu_img), "create",
                "-f", "qcow2", "-F", "qcow2",
                "-b", str(self.settings.base_image), str(disk),
            ])
            self._run([
                str(self.settings.qemu_img), "resize", "-f", "qcow2",
                str(disk), f"{disk_gb}G",
            ])
            self.write_seed(vm)
            self._save(vm)
        except Exception:
            # Clean up partial state so the registry stays consistent.
            import shutil
            shutil.rmtree(vm_dir, ignore_errors=True)
            raise
        return vm

    def write_seed(self, vm: VM) -> Path:
        """(Re)generate the cloud-init seed of a VM from its stored settings. The instance id never changes."""
        return write_seed_iso(
            self.vm_dir(vm.id) / "seed.iso",
            instance_id=vm.id,
            hostname=vm.hostname,
            ssh_authorized_keys=(self.settings.ssh_public_key,) if self.settings.ssh_public_key else (),
            backend_url=vm.backend_url,
            backend_token=vm.backend_token,
        )

    def delete(self, vm: VM) -> None:
        """Stop (if needed) and remove a VM with all its data."""
        if self.is_running(vm):
            self.stop(vm, force=True)
        import shutil
        shutil.rmtree(self.vm_dir(vm.id), ignore_errors=True)

    # -- start / stop -------------------------------------------------------

    def is_running(self, vm: VM) -> bool:
        proc = self._procs.get(vm.id)
        if proc is not None:
            return proc.poll() is None
        # Not started by this process: fall back to the persisted PID so
        # running VMs survive an etcode-manager restart.
        return vm.pid > 0 and _pid_alive(vm.pid)

    def start(self, vm: VM) -> VM:
        """Boot detached QEMU with loopback QMP, VNC and optional SSH forwarding."""
        self.settings.require_tools()
        if self.is_running(vm):
            raise VMError(f"'{vm.name}' is already running.")
        vm_dir = self.vm_dir(vm.id)
        disk, seed = vm_dir / "disk.qcow2", vm_dir / "seed.iso"
        if not disk.exists() or not seed.exists():
            raise VMError(f"VM files missing in {vm_dir}")

        netdev = "user,id=net0"
        with ExitStack() as ports:
            if vm.ssh_port:
                if any(other.id != vm.id and other.ssh_port == vm.ssh_port for other in self.list()):
                    raise VMError(f"SFTP port {vm.ssh_port} is reserved for another VM.")
                try:
                    ports.enter_context(_bind_sftp_port(vm.ssh_port))
                except OSError as exc:
                    raise VMError(
                        f"SFTP port {SFTP_HOST}:{vm.ssh_port} is unavailable. "
                        "Free the port or disable and re-enable file access to get a new address."
                    ) from exc
                netdev += f",hostfwd=tcp:{SFTP_HOST}:{vm.ssh_port}-:22"
            # Pick the dynamic control/display ports while the SSH port is reserved.
            vm.qmp_port = _free_port()
            display, vm.vnc_port = _free_vnc_display()

        log = open(vm_dir / "qemu.log", "ab")
        argv = [
            str(self.settings.qemu_system),
            "-name", f"etcode-{vm.name}",
            "-machine", "q35,accel=kvm:whpx:tcg",  # first available is used
            "-cpu", "max",
            "-smp", str(vm.cpus),
            "-m", str(vm.memory_mb),
            "-drive", f"file={disk},format=qcow2,if=virtio",
            "-drive", f"file={seed},format=raw,if=virtio,readonly=on",
            "-device", "virtio-net-pci,netdev=net0",
            "-netdev", netdev,
            # Advertise a 1080p preferred mode to the guest via the virtual
            # monitor's EDID, so Xfce and QEMU's VNC framebuffer use 1920x1080.
            "-vga", "none",
            "-device", "virtio-vga,edid=on,xres=1920,yres=1080",
            "-display", "none",
            "-vnc", f"127.0.0.1:{display}",  # TCP port 5900+display
            "-qmp", f"tcp:127.0.0.1:{vm.qmp_port},server,nowait",
        ]

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        try:
            proc = subprocess.Popen(
                argv, stdout=log, stderr=log,
                stdin=subprocess.DEVNULL, close_fds=True,
                start_new_session=(sys.platform != "win32"),
                creationflags=creationflags,
            )
        except OSError as exc:
            log.close()
            raise VMError(f"Failed to launch QEMU: {exc}") from exc
        finally:
            log.close()  # the child owns its copy of the descriptor
        self._procs[vm.id] = proc
        vm.pid = proc.pid

        # Wait until QMP accepts connections before reporting success.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise VMError(
                    f"QEMU exited immediately (code {proc.returncode}); "
                    f"see {vm_dir / 'qemu.log'}"
                )
            client = QMPClient(port=vm.qmp_port)
            try:
                client.connect(timeout=2.0)
                break
            except QMPError:
                time.sleep(0.2)
            finally:
                client.close()
        else:
            self.stop(vm, force=True)
            raise VMError("QMP control socket never came up; see qemu.log")

        vm.status = "running"
        self._save(vm)
        return vm

    def stop(self, vm: VM, force: bool = False, timeout: float = 30.0) -> None:
        """Stop a VM: ACPI powerdown first, then QMP quit, then kill.

        Works both for VMs started by this process and for VMs left over
        from a previous etcode-manager run (tracked via the saved PID).
        """
        proc = self._procs.get(vm.id)
        if proc is not None and proc.poll() is not None:
            proc = None
            self._procs.pop(vm.id, None)
        if proc is None and not (vm.pid and _pid_alive(vm.pid)):
            vm.pid, vm.status = 0, "stopped"
            self._save(vm)
            return

        def exited() -> bool:
            if proc is not None:
                return proc.poll() is not None
            return not _pid_alive(vm.pid)

        client = QMPClient(port=vm.qmp_port)
        try:
            client.connect(timeout=5.0)
            if force:
                client.quit()
            else:
                client.powerdown()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline and not exited():
                    time.sleep(0.3)
                if not exited():
                    client.quit()
        except QMPError:
            pass  # QEMU may already have exited, or need process-level termination.
        finally:
            client.close()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not exited():
            time.sleep(0.1)
        if not exited():
            if proc is not None:
                self._terminate(proc)
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5.0)
            elif vm.pid:
                self._kill_pid(vm.pid)

        self._procs.pop(vm.id, None)
        vm.pid, vm.status = 0, "stopped"
        self._save(vm)

    @staticmethod
    def _kill_pid(pid: int) -> None:
        """Terminate a QEMU process we only know by PID."""
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=10)
            else:
                os.kill(pid, signal.SIGTERM)
                time.sleep(1.0)
                if _pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError, subprocess.SubprocessError):
            pass

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if sys.platform == "win32":
            proc.terminate()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _run(argv: list[str]) -> None:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise VMError(f"{' '.join(argv[:2])} failed: {result.stderr.strip() or result.stdout.strip()}")