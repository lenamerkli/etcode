"""The dialog behind 'New session': pick a model, switch web search on or off."""
from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout, QWidget

from ..core.models import ModelInfo
from ..core.options import SessionOptions
from .options_dialog import OptionsEditor


class NewSessionDialog(QDialog):
    def __init__(self, models: Sequence[ModelInfo], last_model: str | None = None, parent: QWidget | None = None,
                 last_options: SessionOptions | None = None):
        super().__init__(parent)
        self.setWindowTitle("New session")
        self.setModal(True)
        self._models = list(models)

        self._combo = QComboBox()
        self._combo.setObjectName("modelCombo")
        for index, model in enumerate(self._models):
            label = model.name + ("  (images)" if model.supports_images else "")
            if not model.available:
                label += "  (not available: the backend has no API key for it)"
            self._combo.addItem(label, model.tech_name)
            if not model.available:
                item = self._combo.model().item(index)  # type: ignore[attr-defined]
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self._web = QCheckBox("Allow web search")
        self._web.setObjectName("webSearch")

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Model", self._combo)
        form.addRow("", self._web)
        self._options = OptionsEditor(last_options)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("Options (anything not set uses the provider's default)"))
        layout.addWidget(self._options)
        self._notice = QLabel()
        self._notice.setObjectName("notice")
        self._notice.setWordWrap(True)
        layout.addWidget(self._notice)
        layout.addWidget(self._buttons)

        available = [i for i, m in enumerate(self._models) if m.available]
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if not available:
            ok.setEnabled(False)
            self._notice.setText("The backend has no model that can be used right now." if self._models
                                 else "The backend returned no models.")
        else:
            preferred = next((i for i in available if self._models[i].tech_name == last_model), available[0])
            self._combo.setCurrentIndex(preferred)
            self._notice.hide()

    def model_name(self) -> str | None:
        """The tech name of the chosen model, or None if there is none to choose."""
        index = self._combo.currentIndex()
        if index < 0 or not self._models[index].available:
            return None
        return self._models[index].tech_name

    def websearch(self) -> bool:
        return self._web.isChecked()

    def options(self) -> SessionOptions:
        return self._options.options()
