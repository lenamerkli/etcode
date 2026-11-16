"""The strip above the message box: which model, whether web search is on, how hard it thinks, and more options.

* The model can be changed while the session is idle (or failed), to a model that takes pictures if the current one
  does, and to one that does not if the current one does not (the backend refuses anything else). The entries
  that cannot be chosen are greyed out, and their tooltip says why.
* Web search is decided when the session is created. It is shown here, not changeable.
* Reasoning effort, temperature and the most tokens are sent with each message, and remembered for the session.
"""
from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QToolButton, QWidget

from ..core.models import ModelInfo, SessionMeta
from ..core.options import EFFORTS, SessionOptions
from .options_dialog import OptionsDialog


class ModelBar(QWidget):
    model_chosen = Signal(str)  # the tech name of the model the user picked
    options_changed = Signal(object)  # SessionOptions

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._models: list[ModelInfo] = []
        self._meta: SessionMeta | None = None
        self._options = SessionOptions()
        self._can_change = False
        self._updating = False

        self._model = QComboBox()
        self._model.setObjectName("modelPicker")
        self._model.setMinimumContentsLength(18)
        self._model.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._model.activated.connect(self._on_model_activated)
        self._web = QCheckBox("Web search")
        self._web.setObjectName("webSearchIndicator")
        self._web.setEnabled(False)
        self._web.setToolTip("Web search is chosen when a session is created")
        self._effort = QComboBox()
        self._effort.setObjectName("effortPicker")
        self._effort.addItem("Effort: default", None)
        for effort in EFFORTS:
            self._effort.addItem(f"Effort: {effort}", effort)
        self._effort.setToolTip("How hard the model thinks before it answers (sent with each message)")
        self._effort.activated.connect(self._on_effort_activated)
        self._more = QToolButton()
        self._more.setObjectName("optionsButton")
        self._more.setText("⚙")
        self._more.setToolTip("Options of this session: temperature, the most tokens to write")
        self._more.clicked.connect(self.edit_options)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 2, 8, 2)
        row.addWidget(self._model)
        row.addWidget(self._web)
        row.addWidget(self._effort)
        row.addWidget(self._more)
        row.addStretch(1)
        self.set_session(None, SessionOptions(), False)

    # --- what it shows ---------------------------------------------------------------------------------------------

    def set_models(self, models: Sequence[ModelInfo] | None) -> None:
        self._models = list(models or [])
        self._refill()

    def set_session(self, meta: SessionMeta | None, options: SessionOptions, can_change: bool) -> None:
        self._meta, self._options, self._can_change = meta, options, can_change and meta is not None
        self._web.setChecked(bool(meta and meta.websearch))
        self._effort.setEnabled(meta is not None)
        self._more.setEnabled(meta is not None)
        self._updating = True
        try:
            self._effort.setCurrentIndex(max(self._effort.findData(options.reasoning_effort), 0))
        finally:
            self._updating = False
        self._refill()

    def _refill(self) -> None:
        current = self._meta.model if self._meta is not None else None
        entries = list(self._models)
        if current and all(m.tech_name != current for m in entries):  # a model that the catalog does not list (any more)
            entries.append(ModelInfo(current, current, 0, False, "", True))
        current_info = next((m for m in entries if m.tech_name == current), None)
        self._updating = True
        try:
            self._model.clear()
            for info in entries:
                self._model.addItem(info.name + ("  (images)" if info.supports_images else ""), info.tech_name)
                item = self._model.model().item(self._model.count() - 1)  # type: ignore[attr-defined]
                reason = self._why_not(info, current_info)
                if reason:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                    item.setToolTip(reason)
            index = self._model.findData(current)
            self._model.setCurrentIndex(index)
            self._model.setEnabled(self._can_change and bool(entries))
            if not self._can_change and self._meta is not None:
                self._model.setToolTip("The model can only be changed while the session is idle")
            else:
                self._model.setToolTip("The model of this session")
        finally:
            self._updating = False

    @staticmethod
    def _why_not(info: ModelInfo, current: ModelInfo | None) -> str:
        if current is not None and info.tech_name == current.tech_name:
            return ""
        if not info.available:
            return "Not available: the backend has no API key for it"
        if current is not None and current.context_length and info.supports_images != current.supports_images:
            return ("This model takes pictures, the current one does not" if info.supports_images
                    else "The current model takes pictures, this one does not")
        return ""

    # --- what the user does ------------------------------------------------------------------------------------------

    def _on_model_activated(self, index: int) -> None:
        if self._updating:
            return
        tech_name = self._model.itemData(index)
        if tech_name and self._meta is not None and tech_name != self._meta.model:
            self.model_chosen.emit(tech_name)
            self._refill()  # the picker shows the model that is really used until the backend says otherwise

    def _on_effort_activated(self, index: int) -> None:
        if self._updating:
            return
        effort = self._effort.itemData(index)
        self._options = SessionOptions(self._options.temperature, self._options.max_tokens, effort)
        self.options_changed.emit(self._options)

    def edit_options(self) -> None:
        dialog = OptionsDialog(self._options, self)
        if dialog.exec() == OptionsDialog.DialogCode.Accepted:
            self._options = dialog.options()
            self.options_changed.emit(self._options)

    # for tests
    @property
    def model_picker(self) -> QComboBox:
        return self._model

    @property
    def effort_picker(self) -> QComboBox:
        return self._effort

    @property
    def web_indicator(self) -> QCheckBox:
        return self._web

    @property
    def options_button(self) -> QToolButton:
        return self._more

    def entry_state(self, tech_name: str) -> tuple[bool, str]:
        """(can be chosen, tooltip) of an entry of the model picker."""
        index = self._model.findData(tech_name)
        item = self._model.model().item(index)  # type: ignore[attr-defined]
        return bool(item.flags() & Qt.ItemFlag.ItemIsEnabled), item.toolTip()
