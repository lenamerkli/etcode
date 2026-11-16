"""A read-only text view that grows to fit its text, for messages in the conversation.

The text comes from a model, so it is untrusted: raw HTML in Markdown is not rendered, no resource
is loaded for it (a Markdown image would otherwise read local files), and a link only opens after
the user confirms it.
"""
from __future__ import annotations

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QTextDocument, QTextOption
from PySide6.QtWidgets import QFrame, QMessageBox, QSizePolicy, QTextBrowser, QWidget

OPENABLE_SCHEMES = ("http", "https", "mailto")


class AutoHeightBrowser(QTextBrowser):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)  # a long URL or hash must not widen the view
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.document().setDocumentMargin(2)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self.anchorClicked.connect(self._on_link)
        self.viewport().setAutoFillBackground(False)
        self._fit()

    def _fit(self) -> None:
        height = int(self.document().size().height()) + 2 * self.frameWidth() + 2
        if height != self.height():
            self.setFixedHeight(max(height, 1))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def loadResource(self, type_: int, name: QUrl):  # noqa: N802 (Qt's name)
        return None  # never load anything for the text: no local files, no network

    # --- content ---------------------------------------------------------------------------------------

    def set_markdown(self, text: str) -> None:
        if text == getattr(self, "_markdown", None):
            return
        self._markdown, self._plain = text, None
        features = QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML
        self.document().setMarkdown(text, features)
        self._fit()

    def set_plain(self, text: str) -> None:
        if text == getattr(self, "_plain", None):
            return
        self._plain, self._markdown = text, None
        self.setPlainText(text)
        self._fit()

    def shown_text(self) -> str:
        return self.toPlainText()

    # --- links -------------------------------------------------------------------------------------------

    def confirm_link(self, url: QUrl) -> bool:
        answer = QMessageBox.question(self, "Open link", f"Open this link in the browser?\n\n{url.toString()}")
        return answer == QMessageBox.StandardButton.Yes

    def _on_link(self, url: QUrl) -> None:
        if url.scheme().lower() in OPENABLE_SCHEMES and self.confirm_link(url):
            QDesktopServices.openUrl(url)
