"""Editing the inference options of a session: how hard the model thinks, temperature, and the most it may write."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget,
)

from ..core.options import EFFORTS, MAX_TEMPERATURE, SessionOptions

DEFAULT_CHOICE = "default"


class OptionsEditor(QWidget):
    """The three options as form fields. A field that is switched off means 'the provider's default'."""

    def __init__(self, options: SessionOptions | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._effort = QComboBox()
        self._effort.setObjectName("effortCombo")
        self._effort.addItem("provider default", None)
        for effort in EFFORTS:
            self._effort.addItem(effort, effort)
        self._temperature_on = QCheckBox("set")
        self._temperature_on.setObjectName("temperatureOn")
        self._temperature = QDoubleSpinBox()
        self._temperature.setObjectName("temperatureValue")
        self._temperature.setRange(0.0, MAX_TEMPERATURE)
        self._temperature.setSingleStep(0.1)
        self._temperature.setDecimals(2)
        self._temperature.setValue(1.0)
        self._temperature.setEnabled(False)
        self._temperature_on.toggled.connect(self._temperature.setEnabled)
        self._tokens_on = QCheckBox("set")
        self._tokens_on.setObjectName("maxTokensOn")
        self._tokens = QSpinBox()
        self._tokens.setObjectName("maxTokensValue")
        self._tokens.setRange(1, 10_000_000)
        self._tokens.setSingleStep(500)
        self._tokens.setValue(4096)
        self._tokens.setEnabled(False)
        self._tokens_on.toggled.connect(self._tokens.setEnabled)

        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Reasoning effort", self._effort)
        row = QHBoxLayout()
        row.addWidget(self._temperature_on)
        row.addWidget(self._temperature, 1)
        form.addRow("Temperature", row)
        row = QHBoxLayout()
        row.addWidget(self._tokens_on)
        row.addWidget(self._tokens, 1)
        form.addRow("Most tokens to write", row)
        self.set_options(options or SessionOptions())

    def set_options(self, options: SessionOptions) -> None:
        index = self._effort.findData(options.reasoning_effort)
        self._effort.setCurrentIndex(max(index, 0))
        self._temperature_on.setChecked(options.temperature is not None)
        if options.temperature is not None:
            self._temperature.setValue(float(options.temperature))
        self._tokens_on.setChecked(options.max_tokens is not None)
        if options.max_tokens is not None:
            self._tokens.setValue(int(options.max_tokens))

    def options(self) -> SessionOptions:
        return SessionOptions(
            temperature=round(self._temperature.value(), 2) if self._temperature_on.isChecked() else None,
            max_tokens=self._tokens.value() if self._tokens_on.isChecked() else None,
            reasoning_effort=self._effort.currentData(),
        )


class OptionsDialog(QDialog):
    def __init__(self, options: SessionOptions, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Options of this session")
        self.setModal(True)
        self._editor = OptionsEditor(options)
        note = QLabel("These are sent with every message you send in this session. Anything that is not set uses the "
                      "default of the model's provider.")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self._editor)
        layout.addWidget(buttons)

    def options(self) -> SessionOptions:
        return self._editor.options()
