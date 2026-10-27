"""Main window: VM list, lifecycle actions and the remote screen."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QLabel, QMessageBox, QSplitter,
    QStatusBar, QSpinBox, QDialog, QDialogButtonBox, QFormLayout,
    QLineEdit,
)

from .config import Settings, detect_accel
from .vm import VM, VMManager, VMError
from .view import VncView


class NewVMDialog(QDialog):
    """Collects name and resources for a new VM."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New VM")
        form = QFormLayout(self)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. task-001")
        form.addRow("Name:", self.name_edit)

        self.cpus = QSpinBox()
        self.cpus.setRange(1, 64)
        self.cpus.setValue(4)
        form.addRow("vCPUs:", self.cpus)

        self.memory = QSpinBox()
        self.memory.setRange(256, 262144)
        self.memory.setSingleStep(1024)
        self.memory.setValue(4096)
        self.memory.setSuffix(" MB")
        form.addRow("Memory:", self.memory)

        self.disk = QSpinBox()
        self.disk.setRange(2, 2048)
        self.disk.setValue(32)
        self.disk.setSuffix(" GB")
        form.addRow("Disk:", self.disk)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple[str, int, int, int]:
        return (
            self.name_edit.text(),
            self.cpus.value(),
            self.memory.value(),
            self.disk.value(),
        )


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, manager: VMManager):
        super().__init__()
        self.settings = settings
        self.manager = manager
        self._current: VM | None = None

        self.setWindowTitle("etcode-manager")
        self.resize(1100, 700)

        # ---- actions
        self.act_new = QAction("New VM…", self)
        self.act_new.triggered.connect(self.on_new)
        self.act_start = QAction("Start", self)
        self.act_start.triggered.connect(self.on_start)
        self.act_stop = QAction("Stop", self)
        self.act_stop.triggered.connect(self.on_stop)
        self.act_delete = QAction("Delete", self)
        self.act_delete.triggered.connect(self.on_delete)

        toolbar = self.addToolBar("VM")
        toolbar.setMovable(False)
        for action in (self.act_new, self.act_start, self.act_stop, self.act_delete):
            toolbar.addAction(action)

        # ---- splitter: VM list | screen
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.vm_list = QListWidget()
        self.vm_list.currentItemChanged.connect(self.on_select)
        splitter.addWidget(self.vm_list)

        right = QWidget()
        layout = QVBoxLayout(right)
        layout.setContentsMargins(4, 4, 4, 4)

        self.info = QLabel("No VM selected")
        layout.addWidget(self.info)

        self.screen = VncView()
        self.screen.connected.connect(self.on_screen_connected)
        self.screen.disconnected.connect(self.on_screen_disconnected)
        self.screen.error.connect(self.on_screen_error)
        layout.addWidget(self.screen, stretch=1)

        conn_row = QHBoxLayout()
        self.btn_connect = QPushButton("Connect screen")
        self.btn_connect.clicked.connect(self.on_connect_screen)
        self.btn_disconnect = QPushButton("Disconnect screen")
        self.btn_disconnect.clicked.connect(self.on_disconnect_screen)
        self.btn_connect.setEnabled(False)
        self.btn_disconnect.setEnabled(False)
        conn_row.addWidget(self.btn_connect)
        conn_row.addWidget(self.btn_disconnect)
        conn_row.addStretch(1)
        layout.addLayout(conn_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 820])

        self.setCentralWidget(splitter)
        self.setStatusBar(QStatusBar())
        self._accel = detect_accel()
        self.statusBar().showMessage(f"Ready — accelerator: {self._accel}")

        self.refresh()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(3000)

    # -- data ---------------------------------------------------------------

    def refresh(self) -> None:
        selected_id = self._current.id if self._current else None
        vms = self.manager.list()
        # Keep signals blocked while the list is rebuilt and the selection is
        # restored: clear() destroys the old items, so setCurrentRow() would
        # make a *new* item current and emit currentItemChanged for the same
        # VM — on_select() would then needlessly tear down the screen.
        self.vm_list.blockSignals(True)
        try:
            self.vm_list.clear()
            select_row = -1
            for i, vm in enumerate(vms):
                icon = "▶" if vm.status == "running" else "■"
                item = QListWidgetItem(f"{icon}  {vm.name}  ({vm.status})")
                item.setData(Qt.ItemDataRole.UserRole, vm.id)
                self.vm_list.addItem(item)
                if vm.id == selected_id:
                    select_row = i
            if select_row >= 0:
                self.vm_list.setCurrentRow(select_row)
            elif vms:
                # Previously selected VM is gone: fall back to the first one.
                self.vm_list.setCurrentRow(0)
                selected_id = vms[0].id
            self._current = next((vm for vm in vms if vm.id == selected_id), None)
        finally:
            self.vm_list.blockSignals(False)
        self.update_info()

    def update_info(self) -> None:
        vm = self._current
        if vm is None:
            self.info.setText("No VM selected")
            return
        self.info.setText(
            f"<b>{vm.name}</b> — {vm.status} — {vm.cpus} vCPU, "
            f"{vm.memory_mb} MB RAM, {vm.disk_gb} GB disk"
            + (f" — VNC port {vm.vnc_port}" if vm.status == "running" else "")
        )
        running = vm.status == "running"
        self.act_start.setEnabled(not running)
        self.act_stop.setEnabled(running)
        self.act_delete.setEnabled(not running)
        self.btn_connect.setEnabled(running)
        self.btn_disconnect.setEnabled(self.screen.is_connected)

    def selected_vm(self) -> VM | None:
        return self._current

    # -- slots --------------------------------------------------------------

    def on_select(self, current: QListWidgetItem | None, previous) -> None:
        vm_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        prev_id = self._current.id if self._current else None
        # Tear the screen down only when the user actually switches to a
        # different VM — not when the same VM is merely re-selected.
        if vm_id != prev_id and self.screen.is_connected:
            self.screen.disconnect_from()
        self._current = next((vm for vm in self.manager.list() if vm.id == vm_id), None)
        self.update_info()

    def on_new(self) -> None:
        dialog = NewVMDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, cpus, memory, disk = dialog.values()
        try:
            self.statusBar().showMessage("Creating VM…")
            vm = self.manager.create(name, cpus=cpus, memory_mb=memory, disk_gb=disk)
        except VMError as exc:
            QMessageBox.warning(self, "Cannot create VM", str(exc))
            self.statusBar().showMessage("Ready")
            return
        self.statusBar().showMessage(f"Created VM '{vm.name}'")
        self.refresh()

    def on_start(self) -> None:
        vm = self.selected_vm()
        if vm is None:
            return
        try:
            self.statusBar().showMessage(f"Starting '{vm.name}'…")
            self.manager.start(vm)
        except VMError as exc:
            QMessageBox.warning(self, "Cannot start VM", str(exc))
        self.statusBar().showMessage("Ready")
        self.refresh()

    def on_stop(self) -> None:
        vm = self.selected_vm()
        if vm is None:
            return
        self.statusBar().showMessage(f"Stopping '{vm.name}'…")
        self.manager.stop(vm)
        self.statusBar().showMessage("Ready")
        self.refresh()

    def on_delete(self) -> None:
        vm = self.selected_vm()
        if vm is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete VM",
            f"Delete '{vm.name}' and all its data (disk, snapshots)?\n"
            "This cannot be undone.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.manager.delete(vm)
        except VMError as exc:
            QMessageBox.warning(self, "Cannot delete VM", str(exc))
        self._current = None
        self.refresh()

    # -- remote screen ------------------------------------------------------

    def on_connect_screen(self) -> None:
        vm = self.selected_vm()
        if vm is None or vm.status != "running":
            return
        self.statusBar().showMessage(f"Connecting to '{vm.name}' screen…")
        self.screen.connect_to("127.0.0.1", vm.vnc_port)
        self.update_info()

    def on_disconnect_screen(self) -> None:
        self.screen.disconnect_from()
        self.update_info()

    def on_screen_connected(self, name: str) -> None:
        self.statusBar().showMessage(f"Screen connected ({name})")
        self.update_info()

    def on_screen_disconnected(self, reason: str) -> None:
        self.statusBar().showMessage(f"Screen disconnected: {reason}")
        self.update_info()

    def on_screen_error(self, message: str) -> None:
        self.statusBar().showMessage(f"Screen error: {message}")
        QMessageBox.warning(self, "Screen error", message)

    def closeEvent(self, event) -> None:
        self.screen.disconnect_from()
        super().closeEvent(event)