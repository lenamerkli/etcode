"""The conversation of one session: notices, the transcript, a status line and the composer."""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..core import session_state as ss
from ..core.options import SessionOptions
from ..core.models import ModelInfo
from ..core.transcript import ADDED, REMOVED, RESET
from .image_loader import ImageLoader
from .model_bar import ModelBar
from .chat_session import ChatSession
from .composer import Composer
from .formatting import status_label
from .transcript_view import TranscriptView

CONTINUE_TEXT = "Please continue."
NOTICE_STYLE = "background: #fff7e0; color: #5c4400; border: 1px solid #f0d58a; border-radius: 4px;"
ERROR_NOTICE_STYLE = "background: #fdecec; color: #8a1f1f; border: 1px solid #f1b5b5; border-radius: 4px;"
_ERROR_KINDS = {ss.NOTICE_UNAVAILABLE, ss.NOTICE_ERROR, ss.NOTICE_LOAD}


class ChatPane(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._chat: ChatSession | None = None
        self._notice_deadline: dict[str, float] = {}
        self._notice_widgets: dict[str, tuple[QFrame, QLabel]] = {}

        self._notices = QVBoxLayout()
        self._notices.setContentsMargins(8, 8, 8, 0)
        self._notices.setSpacing(4)
        self._transcript = TranscriptView()
        self._status = QLabel()
        self._status.setObjectName("statusLine")
        self._cost = QLabel()
        self._cost.setObjectName("costLabel")
        self._cost.setToolTip("What this session has cost so far. A leading ≥ means that some answers could not be priced.")
        self._composer = Composer()
        self._model_bar = ModelBar()
        self._continue = QPushButton("Continue")
        self._continue.setObjectName("continueButton")
        self._continue.setToolTip("The last answer ended with an error. Ask the model to carry on.")
        self._continue.setFlat(True)
        self._continue.setVisible(False)
        self._continue.clicked.connect(self._on_continue)
        self._models: list[ModelInfo] | None = None
        strip = QHBoxLayout()
        strip.setContentsMargins(10, 0, 10, 0)
        strip.addWidget(self._status)
        strip.addWidget(self._continue)
        strip.addStretch(1)
        strip.addWidget(self._cost)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(self._notices)
        layout.addWidget(self._transcript, 1)
        layout.addLayout(strip)
        layout.addWidget(self._model_bar)
        layout.addWidget(self._composer)

        self._transcript.retry_requested.connect(self._resend)
        self._transcript.discard_requested.connect(self._discard)
        self._model_bar.model_chosen.connect(self._on_model_chosen)
        self._model_bar.options_changed.connect(self._on_options_changed)
        self._composer.send_requested.connect(self._on_send)
        self._composer.stop_requested.connect(self._on_stop)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._on_stop)
        self._countdown = QTimer(self)
        self._countdown.setInterval(500)
        self._countdown.timeout.connect(self._tick)
        self.bind(None)

    # --- binding to a session -----------------------------------------------------------------------------------

    @property
    def chat(self) -> ChatSession | None:
        return self._chat

    def bind(self, chat: ChatSession | None) -> None:
        """Show this session. The session keeps running when another one is shown."""
        if self._chat is not None:
            self._chat.state_changed.disconnect(self._refresh_state)
            self._chat.state.model.unsubscribe(self._on_transcript_change)
        self._chat = chat
        self._transcript.set_model(chat.state.model if chat is not None else None)
        if chat is not None:
            chat.state_changed.connect(self._refresh_state)
            chat.state.model.subscribe(self._on_transcript_change)
        self._refresh_state()
        if chat is not None:
            self._composer.focus_input()

    def _on_transcript_change(self, change) -> None:
        """A message was added or removed: whether the last answer is an error may have changed. (Streaming only updates a
        message, so this is not called for every piece of text.)"""
        if self._chat is not None and change.kind in (ADDED, REMOVED, RESET):
            self._continue.setVisible(self._chat.state.can_continue)

    def set_models(self, models: list[ModelInfo] | None) -> None:
        """The catalog of the backend: it fills the model picker, and says whether the session's model takes pictures."""
        self._models = models
        self._model_bar.set_models(models)
        self._refresh_state()

    def set_image_loader(self, loader: ImageLoader | None) -> None:
        self._transcript.set_image_loader(loader)

    def _model_takes_images(self, state: ss.SessionState) -> tuple[bool, str]:
        """Whether the session's model can look at pictures. Unknown (no catalog yet) counts as yes: the backend decides."""
        meta = state.meta
        if meta is None or not self._models:
            return True, ""
        info = next((m for m in self._models if m.tech_name == meta.model), None)
        if info is None or info.supports_images:
            return True, ""
        return False, f"{info.name} cannot look at pictures. Choose a model with the (images) mark to send pictures."

    # --- from the user ---------------------------------------------------------------------------------------------

    def _on_send(self, text: str) -> None:
        if self._chat is None or not self._chat.state.can_send:
            return
        attachments = self._composer.attachments()
        if attachments:
            takes, reason = self._model_takes_images(self._chat.state)
            if not takes:  # the model was changed after the pictures were attached
                self._composer.show_problem(reason)
                return
        self._composer.clear()
        self._chat.send(text, attachments)
        self._transcript.scroll_to_bottom()

    def _on_continue(self) -> None:
        if self._chat is not None and self._chat.state.can_continue:
            self._chat.send(CONTINUE_TEXT)
            self._transcript.scroll_to_bottom()

    def _on_model_chosen(self, tech_name: str) -> None:
        if self._chat is not None:
            self._chat.change_model(tech_name)

    def _on_options_changed(self, options) -> None:
        if self._chat is not None:
            self._chat.set_options(options)

    def _resend(self, key: str) -> None:
        """Send a message again that did not arrive. It leaves its place and is sent as a new message."""
        chat = self._chat
        entry = chat.state.model.get(key) if chat is not None else None
        if chat is None or entry is None or entry.role != "user" or entry.state != "failed" or not chat.state.can_send:
            return
        chat.state.model.discard(key)
        hashes = entry.image_hashes
        chat.send(entry.text, () if hashes else entry.attachments, hashes)  # pictures that are not on the backend yet go up again
        self._transcript.scroll_to_bottom()

    def _discard(self, key: str) -> None:
        if self._chat is not None:
            self._chat.state.model.discard(key)

    def _on_stop(self) -> None:
        if self._chat is not None and self._chat.state.busy:
            self._chat.stop()

    # --- drawing the state ---------------------------------------------------------------------------------------------

    def _refresh_state(self) -> None:
        chat = self._chat
        if chat is None:
            self._status.setText("")
            self._cost.setText("")
            self._composer.set_sending_allowed(False)
            self._composer.set_busy(False)
            self._model_bar.set_session(None, SessionOptions(), False)
            self._continue.setVisible(False)
            self._sync_notices({})
            return
        state = chat.state
        takes, reason = self._model_takes_images(state)
        self._composer.set_images_allowed(takes, reason)
        self._model_bar.set_session(state.meta, chat.options, state.loaded and state.can_send and state.status in ("idle", "failed"))
        self._continue.setVisible(state.can_continue)
        self._composer.set_busy(state.busy)
        self._composer.set_sending_allowed(state.can_send, "This session cannot be used any more." if not state.can_send else "")
        self._status.setText(self._status_text(state))
        self._cost.setText(state.cost_text())
        self._sync_notices(state.notices)

    @staticmethod
    def _status_text(state: ss.SessionState) -> str:
        if not state.loaded:
            return "Loading…" if ss.NOTICE_LOAD not in state.notices else "Could not load"
        text = status_label(state.status)
        if state.queued_count:
            text += f" · {state.queued_count} message{'s' if state.queued_count != 1 else ''} queued"
        return text

    def _sync_notices(self, notices: dict[str, ss.Notice]) -> None:
        for kind in list(self._notice_widgets):
            if kind not in notices:
                frame, _ = self._notice_widgets.pop(kind)
                self._notices.removeWidget(frame)
                frame.setParent(None)
                frame.deleteLater()
                self._notice_deadline.pop(kind, None)
        for kind, notice in notices.items():
            if kind not in self._notice_widgets:
                self._notice_widgets[kind] = self._make_notice(kind)
            frame, label = self._notice_widgets[kind]
            if notice.retry_in is not None:
                self._notice_deadline[kind] = time.monotonic() + notice.retry_in
            label.setText(self._notice_text(notice))
        if self._notice_deadline:
            self._countdown.start()
        else:
            self._countdown.stop()

    def _make_notice(self, kind: str) -> tuple[QFrame, QLabel]:
        frame = QFrame()
        frame.setObjectName(f"notice_{kind}")
        frame.setStyleSheet(ERROR_NOTICE_STYLE if kind in _ERROR_KINDS else NOTICE_STYLE)
        label = QLabel()
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row = QHBoxLayout(frame)
        row.setContentsMargins(8, 4, 8, 4)
        row.addWidget(label, 1)
        if kind == ss.NOTICE_LOAD:
            retry = QPushButton("Try again")
            retry.setObjectName("retryButton")
            retry.clicked.connect(self._retry_load)
            row.addWidget(retry)
        if kind == ss.NOTICE_CONNECTION:
            now = QPushButton("Retry now")
            now.setObjectName("retryNowButton")
            now.clicked.connect(lambda: self._chat and self._chat.retry_now())
            row.addWidget(now)
        if kind == ss.NOTICE_UNAVAILABLE:
            again = QPushButton("Try again")
            again.setObjectName("recoverButton")
            again.clicked.connect(lambda: self._chat and self._chat.recover())
            row.addWidget(again)
        if kind == ss.NOTICE_ERROR:
            close = QPushButton("Dismiss")
            close.setObjectName("dismissButton")
            close.clicked.connect(lambda: self._chat and self._chat.state.dismiss(kind))
            row.addWidget(close)
        self._notices.addWidget(frame)
        return frame, label

    def _retry_load(self) -> None:
        if self._chat is not None:
            self._chat.refresh()

    def _notice_text(self, notice: ss.Notice) -> str:
        if notice.kind == ss.NOTICE_CONNECTION:
            remaining = max(int(round(self._notice_deadline.get(notice.kind, 0) - time.monotonic())), 0)
            wait = f" Trying again in {remaining} s." if remaining else " Trying again…"
            return f"Connection to the backend lost: {notice.text}.{wait}".replace("..", ".")
        return notice.text

    def _tick(self) -> None:
        if self._chat is None:
            return
        for kind, (_, label) in self._notice_widgets.items():
            notice = self._chat.state.notices.get(kind)
            if notice is not None and kind in self._notice_deadline:
                label.setText(self._notice_text(notice))

    # --- for tests -----------------------------------------------------------------------------------------------------------

    @property
    def model_bar(self) -> ModelBar:
        return self._model_bar

    @property
    def continue_button(self) -> QPushButton:
        return self._continue

    @property
    def transcript(self) -> TranscriptView:
        return self._transcript

    @property
    def composer(self) -> Composer:
        return self._composer

    @property
    def status_text(self) -> str:
        return self._status.text()

    @property
    def cost_text(self) -> str:
        return self._cost.text()

    def notice_kinds(self) -> list[str]:
        return list(self._notice_widgets)

    def notice_text(self, kind: str) -> str:
        return self._notice_widgets[kind][1].text()

    def notice_frame(self, kind: str) -> QFrame:
        return self._notice_widgets[kind][0]

