"""Main window: VM list, lifecycle actions and the remote screen."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QLabel, QMessageBox, QSplitter,
    QStatusBar, QSpinBox, QDialog, QDialogButtonBox, QFormLayout,
    QLineEdit, QCheckBox, QComboBox,
)

from .config import (
    DEFAULT_BACKEND_URL, Settings, detect_accel, normalize_backend_token, normalize_backend_url,
    normalize_scaling, save_settings,
)
from .vm import VM, VMManager, VMError
from .view import VncView
from .scaling import ScalingFilter


class NewVMDialog(QDialog):
    """Collects name, resources and the backend settings for a new VM.

    The backend URL and token are what the etcode desktop app inside the VM will use. They are prefilled with the
    defaults from ``config.json`` and can be changed per VM (for example to give one VM its own token).
    """

    def __init__(self, parent=None, backend_url: str = DEFAULT_BACKEND_URL, backend_token: str = ""):
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

        self.backend_url_edit = QLineEdit(backend_url)
        self.backend_url_edit.setObjectName("backendUrl")
        self.backend_url_edit.setPlaceholderText(DEFAULT_BACKEND_URL)
        self.backend_url_edit.setToolTip(
            "Where the etcode desktop app in the VM finds the backend. "
            "The host is 10.0.2.2 as seen from the VM."
        )
        form.addRow("Backend URL:", self.backend_url_edit)

        self.token_edit = QLineEdit(backend_token)
        self.token_edit.setObjectName("backendToken")
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("none: the app asks for it")
        self.token_edit.setToolTip(
            "The access token for the backend. It is stored in this VM's vm.json and seed.iso (both private to "
            "you) and written to /etc/etcode/desktop.json in the VM, where the model can read it. "
            "Use a token that is only for etcode VMs."
        )
        form.addRow("Backend token:", self.token_edit)

        self.show_token = QCheckBox("Show the token")
        self.show_token.setObjectName("showToken")
        self.show_token.toggled.connect(
            lambda shown: self.token_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
            )
        )
        form.addRow("", self.show_token)

        self.error_label = QLabel()
        self.error_label.setObjectName("backendError")
        self.error_label.setStyleSheet("color: #b00020;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        form.addRow(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:
        """Close only if the backend settings are valid; otherwise say what is wrong and stay open."""
        try:
            self.backend_values()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return
        super().accept()

    def values(self) -> tuple[str, int, int, int]:
        return (
            self.name_edit.text(),
            self.cpus.value(),
            self.memory.value(),
            self.disk.value(),
        )

    def backend_values(self) -> tuple[str, str]:
        """``(backend_url, backend_token)``, cleaned. Raises ``ValueError`` (with a message for the user)."""
        return (
            normalize_backend_url(self.backend_url_edit.text()),
            normalize_backend_token(self.token_edit.text()),
        )


class SettingsDialog(QDialog):
    """Edits the defaults that apply to every VM created from now on, and the screen scaling default.

    The backend token is the same secret that each VM receives (in its ``vm.json`` and cloud-init seed). It is
    shown masked by default. Saving writes ``config.json`` privately, because that file now holds the token.
    """

    def __init__(
        self,
        parent=None,
        backend_url: str = DEFAULT_BACKEND_URL,
        backend_token: str = "",
        scaling: ScalingFilter | str = ScalingFilter.AREA,
    ):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        form = QFormLayout(self)

        intro = QLabel(
            "These defaults are used for VMs you create from now on. "
            "Existing VMs keep the settings they were created with."
        )
        intro.setWordWrap(True)
        form.addRow(intro)

        self.backend_url_edit = QLineEdit(backend_url)
        self.backend_url_edit.setObjectName("defaultBackendUrl")
        self.backend_url_edit.setPlaceholderText(DEFAULT_BACKEND_URL)
        self.backend_url_edit.setToolTip(
            "Where the etcode desktop app in a new VM finds the backend. "
            "The host is 10.0.2.2 as seen from the VM."
        )
        form.addRow("Default backend URL:", self.backend_url_edit)

        self.token_edit = QLineEdit(backend_token)
        self.token_edit.setObjectName("defaultBackendToken")
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("none: each VM's app asks for one")
        self.token_edit.setToolTip(
            "The access token that new VMs use. It is stored in config.json (private to you) and copied into "
            "each new VM's vm.json and seed.iso, where the model can read it. Use a token that is only for "
            "etcode VMs."
        )
        form.addRow("Default backend token:", self.token_edit)

        self.show_token = QCheckBox("Show the token")
        self.show_token.setObjectName("showDefaultToken")
        self.show_token.toggled.connect(
            lambda shown: self.token_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
            )
        )
        form.addRow("", self.show_token)

        self.scaling_combo = QComboBox()
        self.scaling_combo.setObjectName("defaultScaling")
        self.scaling_combo.addItem("Smooth (area)", ScalingFilter.AREA)
        self.scaling_combo.addItem("Sharper (Lanczos)", ScalingFilter.LANCZOS)
        self.scaling_combo.setToolTip(
            "The screen filter for this and future manager windows. "
            "Smooth is area-averaged and fast; Sharper uses Lanczos-3 and costs more CPU."
        )
        index = self.scaling_combo.findData(ScalingFilter(normalize_scaling(scaling)))
        self.scaling_combo.setCurrentIndex(max(0, index))
        form.addRow("Default scaling:", self.scaling_combo)

        self.error_label = QLabel()
        self.error_label.setObjectName("settingsError")
        self.error_label.setStyleSheet("color: #b00020;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        form.addRow(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:
        """Close only if every value is valid; otherwise keep the dialog open and explain why."""
        try:
            self.values()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return
        super().accept()

    def values(self) -> tuple[str, str, ScalingFilter]:
        """``(backend_url, backend_token, scaling)``, cleaned. Raises ``ValueError`` with a user-facing message."""
        return (
            normalize_backend_url(self.backend_url_edit.text()),
            normalize_backend_token(self.token_edit.text()),
            ScalingFilter(self.scaling_combo.currentData()),
        )

    # A descriptive alias; there are several defaults, and callers read better with it.
    def defaults(self) -> tuple[str, str, ScalingFilter]:
        return self.values()


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, manager: VMManager):
        super().__init__()
        self.settings = settings
        self.manager = manager
        self._current: VM | None = None

        self.setWindowTitle("etcode-manager")
        self.resize(self._initial_window_size())

        # ---- actions
        self.act_new = QAction("New VM…", self)
        self.act_new.triggered.connect(self.on_new)
        self.act_start = QAction("Start", self)
        self.act_start.triggered.connect(self.on_start)
        self.act_stop = QAction("Stop", self)
        self.act_stop.triggered.connect(self.on_stop)
        self.act_delete = QAction("Delete", self)
        self.act_delete.triggered.connect(self.on_delete)
        self.act_settings = QAction("Settings…", self)
        self.act_settings.setShortcut("Ctrl+,")
        self.act_settings.triggered.connect(self.on_settings)

        toolbar = self.addToolBar("VM")
        toolbar.setMovable(False)
        for action in (self.act_new, self.act_start, self.act_stop, self.act_delete):
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction(self.act_settings)

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

        self.screen = VncView(scaling_filter=self.settings.scaling_filter)
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

        scaling_label = QLabel("Scaling:")
        self.scaling_combo = QComboBox()
        self.scaling_combo.setObjectName("screenScaling")
        self.scaling_combo.addItem("Smooth (area)", ScalingFilter.AREA)
        self.scaling_combo.addItem("Sharper (Lanczos)", ScalingFilter.LANCZOS)
        self.scaling_combo.setToolTip(
            "Smooth: area-averaged downscaling, fast and stable for small text.\n"
            "Sharper: antialiased Lanczos-3, crisper edges but more CPU use and possible faint halos.\n"
            "The default for this and future windows can be changed in Settings."
        )
        self.scaling_combo.setCurrentIndex(
            max(0, self.scaling_combo.findData(self.settings.scaling_filter))
        )
        scaling_label.setBuddy(self.scaling_combo)
        self.scaling_combo.currentIndexChanged.connect(
            lambda _: self.screen.set_scaling_filter(self.scaling_combo.currentData())
        )
        conn_row.addWidget(scaling_label)
        conn_row.addWidget(self.scaling_combo)
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

    @staticmethod
    def _initial_window_size() -> QSize:
        """Open with enough room to show the 1920x1080 guest near its real size.

        The remote screen is fitted into the pane, so a small default window
        scales it down to roughly 40% and normal UI text becomes unreadable
        even with smooth scaling.  Prefer a window that fits the guest 1:1,
        but never open larger than the available screen.
        """
        fallback = QSize(1100, 700)
        screen = QApplication.primaryScreen()
        if screen is None:
            return fallback
        available = screen.availableGeometry()
        if available.width() <= 0 or available.height() <= 0:
            return fallback
        # VM list (280) + guest width (1920) + splitter/margins; the window
        # chrome (toolbar, info line, buttons, status bar) needs ~150 px.
        width = min(available.width(), max(fallback.width(), 280 + 1920 + 40))
        height = min(available.height(), max(fallback.height(), 1080 + 150))
        return QSize(width, height)

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
            + (f"<br>Backend: {html.escape(vm.backend_url)}"
               f" ({'token set' if vm.backend_token else 'no token'})" if vm.backend_url else "")
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
        dialog = NewVMDialog(
            self, backend_url=self.settings.backend_url, backend_token=self.settings.backend_token
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, cpus, memory, disk = dialog.values()
        backend_url, backend_token = dialog.backend_values()
        try:
            self.statusBar().showMessage("Creating VM…")
            vm = self.manager.create(
                name, cpus=cpus, memory_mb=memory, disk_gb=disk,
                backend_url=backend_url, backend_token=backend_token,
            )
        except VMError as exc:
            QMessageBox.warning(self, "Cannot create VM", str(exc))
            self.statusBar().showMessage("Ready")
            return
        self.statusBar().showMessage(f"Created VM '{vm.name}'")
        self.refresh()

    def on_settings(self) -> None:
        """Edit the defaults for future VMs and the screen filter; persist them to config.json."""
        dialog = SettingsDialog(
            self,
            backend_url=self.settings.backend_url,
            backend_token=self.settings.backend_token,
            scaling=self.settings.scaling_filter,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        backend_url, backend_token, scaling = dialog.values()
        previous = (self.settings.backend_url, self.settings.backend_token, self.settings.scaling)
        self.settings.backend_url = backend_url
        self.settings.backend_token = backend_token
        self.settings.scaling_filter = scaling
        try:
            save_settings(self.settings)
        except (RuntimeError, OSError, ValueError) as exc:
            # Keep the in-memory defaults consistent with what is actually on disk.
            self.settings.backend_url, self.settings.backend_token, self.settings.scaling = previous
            self._select_scaling(self.settings.scaling_filter)
            QMessageBox.warning(self, "Cannot save settings", str(exc))
            return
        self._select_scaling(scaling)
        self.statusBar().showMessage("Settings saved")

    def _select_scaling(self, scaling: ScalingFilter) -> None:
        """Select a filter in the screen selector, which also applies it to the current screen."""
        index = self.scaling_combo.findData(scaling)
        if index >= 0:
            self.scaling_combo.setCurrentIndex(index)
        else:
            self.screen.set_scaling_filter(scaling)

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