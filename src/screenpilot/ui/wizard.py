"""First-run wizard: welcome, language, shortcuts, how it works."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QVBoxLayout, QWizard, QWizardPage

from screenpilot.ui.controller import AppController
from screenpilot.ui.qt_helpers import Retranslator, app_icon

SHOWN_SHORTCUTS = ("capture.region", "capture.window", "capture.fullscreen", "capture.all_monitors", "capture.delayed")


class FirstRunWizard(QWizard):
    def __init__(self, controller: AppController) -> None:
        super().__init__(None)
        self.controller = controller
        self.app = controller.app
        self.t = self.app.translator.t
        self.rt = rt = Retranslator(self.app.translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowIcon(app_icon())
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setMinimumSize(560, 420)
        rt.bind(self.setWindowTitle, "wizard.title")
        for button, key in (
            (QWizard.WizardButton.NextButton, "wizard.next"),
            (QWizard.WizardButton.BackButton, "wizard.back"),
            (QWizard.WizardButton.FinishButton, "wizard.finish"),
        ):
            rt.bind(lambda text, b=button: self.setButtonText(b, text), key)

        self.addPage(self._text_page("wizard.welcome.title", "wizard.welcome.body"))
        self.addPage(self._language_page())
        self.addPage(self._shortcuts_page())
        self.addPage(self._text_page("wizard.workflow.title", "wizard.workflow.body", hotkey=True))
        self.finished.connect(self._done)

    def _region_hotkey(self) -> dict:
        return {"hotkey": str(self.app.hotkeys.binding("capture.region") or "—")}

    def _text_page(self, title_key: str, body_key: str, hotkey: bool = False) -> QWizardPage:
        page = QWizardPage()
        self.rt.bind(page.setTitle, title_key)
        body = QLabel()
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.PlainText)
        self.rt.bind(body.setText, body_key, params_fn=self._region_hotkey if hotkey else None)
        QVBoxLayout(page).addWidget(body)
        return page

    def _language_page(self) -> QWizardPage:
        page = QWizardPage()
        self.rt.bind(page.setTitle, "wizard.language.title")
        layout = QVBoxLayout(page)
        body = QLabel()
        body.setWordWrap(True)
        self.rt.bind(body.setText, "wizard.language.body")
        combo = QComboBox()
        for code, name in self.app.translator.available_languages().items():
            combo.addItem(name, code)
        combo.setCurrentIndex(max(0, combo.findData(self.app.translator.language)))
        combo.currentIndexChanged.connect(lambda _i: self.app.settings.set("general.language", combo.currentData()))
        layout.addWidget(combo)
        layout.addWidget(body)
        layout.addStretch(1)
        self.language_combo = combo
        return page

    def _shortcuts_page(self) -> QWizardPage:
        page = QWizardPage()
        self.rt.bind(page.setTitle, "wizard.hotkeys.title")
        layout = QVBoxLayout(page)
        body = QLabel()
        body.setWordWrap(True)
        self.rt.bind(body.setText, "wizard.hotkeys.body")
        layout.addWidget(body)
        form = QFormLayout()
        for action in SHOWN_SHORTCUTS:
            name = QLabel()
            self.rt.bind(name.setText, f"action.{action}")
            key = QLabel(str(self.app.hotkeys.binding(action) or "—"))
            key.setStyleSheet("font-weight: 600;")
            form.addRow(name, key)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    def _done(self, _result: int) -> None:
        self.app.settings.set("general.first_run_completed", True)
        try:
            self.app.settings.save()
        except OSError:
            pass
