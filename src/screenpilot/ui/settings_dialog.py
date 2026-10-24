"""Settings window: general, capture, after-capture actions, hotkeys.

Every change is applied and saved immediately (no OK/Apply), and every label
follows the language live.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QScrollArea,
    QStackedWidget,
    QFrame,
    QVBoxLayout,
    QWidget,
)

from screenpilot.capture.image import CaptureResult
from screenpilot.core.errors import HotkeyError
from screenpilot.hotkeys import Hotkey, RegistrationStatus, describe_conflicts
from screenpilot.ui.controller import AppController
from screenpilot.ui import icons
from screenpilot.ui.qt_helpers import Retranslator, app_icon
from screenpilot.ui.widgets import Card, IconBadge, SettingRow, ToggleSwitch, label, on_theme_changed
from screenpilot.workflow import render_filename

log = logging.getLogger(__name__)

IMAGE_FORMATS = ("png", "jpg", "bmp")
FFMPEG_INSTALL_COMMAND = "winget install --id Gyan.FFmpeg -e"
UI_WORKFLOW_ACTIONS = ("save", "save_as", "copy_image", "copy_path", "open_folder", "open_editor")
FILENAME_TOKENS = "{date} {time} {datetime} {mode} {width} {height} {window}"
STATUS_KEYS = {
    RegistrationStatus.REGISTERED: "hotkeys.status.registered",
    RegistrationStatus.FAILED: "hotkeys.status.failed",
    RegistrationStatus.UNASSIGNED: "hotkeys.status.unassigned",
    RegistrationStatus.PENDING: "hotkeys.status.unassigned",
}


def qt_sequence_to_hotkey(sequence: QKeySequence) -> Hotkey:
    """Convert the first chord of a QKeySequence to our Hotkey (raises HotkeyError)."""
    if sequence.isEmpty():
        raise HotkeyError("Empty key sequence")
    first = QKeySequence(sequence[0]).toString(QKeySequence.SequenceFormat.PortableText)
    return Hotkey.parse(first.replace("Meta", "Win"))


class SettingsDialog(QDialog):
    def __init__(self, controller: AppController) -> None:
        super().__init__(None)
        self.controller = controller
        self.app = controller.app
        self.settings = self.app.settings
        self.t = self.app.translator.t
        self.rt = rt = Retranslator(self.app.translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(860, 600)
        self.rt.bind(self.setWindowTitle, "settings.title")

        # Windows 11 Settings layout: navigation on the left, pages of cards on the right.
        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFixedWidth(210)
        self.nav.setIconSize(QSize(18, 18))
        self.stack = QStackedWidget()
        self.page_titles: list[QLabel] = []
        self.page_index: dict[str, int] = {}
        for icon_name, key, builder in (
            ("general", "settings.nav.general", self._build_general),
            ("region", "settings.nav.capture", self._build_capture),
            ("sparkle", "settings.nav.modes", self._build_modes),
            ("workflow", "settings.nav.after_capture", self._build_after_capture),
            ("video", "settings.nav.recording", self._build_recording),
            ("keyboard", "settings.nav.hotkeys", self._build_hotkeys),
            ("settings", "settings.nav.diagnostics", self._build_diagnostics),
        ):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, icon_name)
            self.nav.addItem(item)
            self.rt.bind(item.setText, key)
            wrapped = self._page(key, builder())
            self.page_index[key.rsplit(".", 1)[-1]] = self.stack.count()
            self.stack.addWidget(wrapped)
            if key == "settings.nav.diagnostics":
                self._diag_page = wrapped
        on_theme_changed(self, self._retheme_nav)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        side = QVBoxLayout()
        side.setContentsMargins(12, 16, 8, 16)
        header = QHBoxLayout()
        header.addWidget(IconBadge("logo", 22, token="accent"))
        header.addWidget(label("ScreenPilot", "subtitle"), 1)
        side.addLayout(header)
        side.addSpacing(12)
        side.addWidget(self.nav, 1)
        close = QPushButton()
        self.rt.bind(close.setText, "settings.close")
        close.clicked.connect(self.close)
        side.addWidget(close)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        policy = self.app.policy
        if policy.active:
            banner = label(role="secondary", wrap=True)
            self.rt.bind(banner.setText, "policy.banner", message=policy.message)
            banner.setStyleSheet("padding: 10px 28px; background: rgba(255,185,0,0.18);")
            right.addWidget(banner)
            self.policy_banner = banner
        right.addWidget(self.stack, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(side)
        layout.addLayout(right, 1)

    def show_page(self, name: str) -> None:
        """Select a page by its short name: general, capture, modes, after_capture, recording, hotkeys, diagnostics."""
        index = self.page_index.get(name)
        if index is not None:
            self.nav.setCurrentRow(index)

    @property
    def tabs(self) -> "SettingsDialog._NavAdapter":
        """Back-compat with the previous tabbed layout (tests, automation)."""
        return SettingsDialog._NavAdapter(self)

    class _NavAdapter:
        def __init__(self, dialog: "SettingsDialog") -> None:
            self._d = dialog

        def setCurrentIndex(self, index: int) -> None:  # noqa: N802
            self._d.nav.setCurrentRow(index)

        def tabText(self, index: int) -> str:  # noqa: N802
            return self._d.nav.item(index).text()

    def _retheme_nav(self, t) -> None:
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            item.setIcon(icons.icon(item.data(Qt.ItemDataRole.UserRole), t.text_secondary, 18))

    def _page(self, title_key: str, content: QWidget) -> QWidget:
        page = QWidget()
        page.setProperty("page", True)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 22, 28, 20)
        outer.setSpacing(14)
        title = label(role="display")
        self.rt.bind(title.setText, title_key)
        outer.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        return page

    def _row(self, icon_name: str | None, title_key: str, desc_key: str | None, control: QWidget | None, **params) -> SettingRow:
        row = SettingRow(icon_name, control)
        self.rt.bind(row.title.setText, title_key, **params)
        if desc_key:
            self.rt.bind(row.description.setText, desc_key, **params)
        else:
            row.description.hide()
        return row

    def _toggle(self, key: str) -> ToggleSwitch:
        toggle = ToggleSwitch()
        toggle.setChecked(bool(self.settings.get(key)))
        toggle.toggled.connect(lambda checked: self._set(key, checked))
        return self._lock(toggle, key)

    def _lock(self, widget: QWidget, key: str) -> QWidget:
        """Grey out a control whose setting is enforced by the administrator."""
        if self.settings.is_locked(key):
            widget.setEnabled(False)
            widget.setToolTip(self.t("error.locked"))
        return widget

    @staticmethod
    def _column() -> tuple[QWidget, QVBoxLayout]:
        widget = QWidget()
        column = QVBoxLayout(widget)
        column.setContentsMargins(0, 0, 6, 0)
        column.setSpacing(6)
        return widget, column

    def _set(self, key: str, value) -> None:
        self.settings.set(key, value)
        try:
            self.settings.save()
        except OSError:
            log.exception("Could not save settings")

    def _label(self, key: str, **params) -> QLabel:
        label = QLabel()
        self.rt.bind(label.setText, key, **params)
        return label

    # -- general --------------------------------------------------------------
    def _build_general(self) -> QWidget:
        page, column = self._column()
        self.language = QComboBox()
        self.language.setMinimumWidth(180)
        for code, name in self.app.translator.available_languages().items():
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(self.app.translator.language)))
        self.language.currentIndexChanged.connect(
            lambda _i: self._set("general.language", self.language.currentData())
        )
        self._lock(self.language, "general.language")
        column.addWidget(self._row("language", "settings.language", "settings.language.desc", self.language))

        self.theme_combo = QComboBox()
        self.theme_combo.setMinimumWidth(180)
        for mode in ("system", "light", "dark"):
            self.theme_combo.addItem("", mode)
        self._retranslate_theme_combo()
        self.rt.on_change(self._retranslate_theme_combo)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(self.settings.get("general.theme"))))
        self.theme_combo.currentIndexChanged.connect(lambda _i: self._set("general.theme", self.theme_combo.currentData()))
        column.addWidget(self._row("theme", "settings.theme", "settings.theme.desc", self.theme_combo))

        column.addWidget(
            self._row("image", "settings.preview", "settings.preview.desc", self._toggle("capture.show_preview"))
        )
        self.history_limit = QSpinBox()
        self.history_limit.setRange(10, 1000)
        self.history_limit.setSingleStep(10)
        self.history_limit.setValue(self.settings.get("history.limit"))
        self.history_limit.valueChanged.connect(lambda v: self._set("history.limit", v))
        column.addWidget(
            self._row("history", "settings.history_limit", "settings.history_limit.desc", self.history_limit)
        )
        column.addWidget(
            self._row("chevron_down", "settings.edge_bar", "settings.edge_bar.desc", self._toggle("general.edge_bar"))
        )
        customize = QPushButton()
        self.rt.bind(customize.setText, "settings.edge_bar.customize")
        customize.clicked.connect(lambda: self.controller.customize_edge_bar(self))
        column.addWidget(self._row("settings", "settings.edge_bar.buttons", "settings.edge_bar.buttons.desc", customize))
        column.addWidget(
            self._row(
                "edit", "settings.editor_open", "settings.editor_open.desc",
                self._combo("editor.open_mode", [("", "ask"), ("", "last")], tr_prefix="settings.editor_open"),
            )
        )
        autostart_toggle = self._toggle("general.start_with_windows")
        from screenpilot.platform import devpilot

        if sys.platform != "win32":
            autostart_toggle.setEnabled(False)
        elif devpilot.owns("autostart"):
            autostart_toggle.setEnabled(False)
            self.rt.bind(autostart_toggle.setToolTip, "settings.start_with_windows.devpilot")
        column.addWidget(
            self._row("logo", "settings.start_with_windows", "settings.start_with_windows.desc", autostart_toggle)
        )
        column.addWidget(
            self._row(
                "window", "settings.start_minimized", "settings.start_minimized.desc", self._toggle("general.start_minimized")
            )
        )
        column.addWidget(
            self._row("check", "settings.tester_mode", "settings.tester_mode.desc", self._toggle("general.tester_mode"))
        )
        column.addSpacing(10)
        privacy = QHBoxLayout()
        privacy.addWidget(IconBadge("shield", 16, token="success"))
        note = label(role="caption", wrap=True)
        self.rt.bind(note.setText, "settings.privacy_note")
        privacy.addWidget(note, 1)
        column.addLayout(privacy)
        from screenpilot import __version__

        version = label(role="caption")
        version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.rt.bind(version.setText, "settings.version", version=__version__)
        column.addWidget(version)
        self.version_label = version
        column.addStretch(1)
        return page

    def _retranslate_theme_combo(self) -> None:
        for i in range(self.theme_combo.count()):
            self.theme_combo.setItemText(i, self.t(f"settings.theme.{self.theme_combo.itemData(i)}"))

    # -- capture --------------------------------------------------------------
    def _build_capture(self) -> QWidget:
        page, column = self._column()

        self.image_format = QComboBox()
        for fmt in IMAGE_FORMATS:
            self.image_format.addItem(fmt.upper(), fmt)
        self.image_format.setCurrentIndex(max(0, self.image_format.findData(self.settings.get("capture.image_format"))))
        self.image_format.currentIndexChanged.connect(self._on_format)
        self._lock(self.image_format, "capture.image_format")
        column.addWidget(self._row("image", "settings.image_format", "settings.image_format.desc", self.image_format))

        self.jpeg_quality = QSpinBox()
        self.jpeg_quality.setRange(30, 100)
        self.jpeg_quality.setSuffix(" %")
        self.jpeg_quality.setValue(self.settings.get("capture.jpeg_quality"))
        self.jpeg_quality.valueChanged.connect(lambda v: self._set("capture.jpeg_quality", v))
        column.addWidget(self._row(None, "settings.jpeg_quality", None, self.jpeg_quality))

        self.delay = QSpinBox()
        self.delay.setRange(1, 60)
        self.delay.setValue(self.settings.get("capture.default_delay_seconds"))
        self.rt.bind(self.delay.setSuffix, "settings.seconds_suffix")
        self.delay.valueChanged.connect(lambda v: self._set("capture.default_delay_seconds", v))
        column.addWidget(self._row("timer", "settings.default_delay", "settings.default_delay.desc", self.delay))
        column.addWidget(
            self._row(
                "region", "settings.region_action", "settings.region_action.desc",
                self._combo("capture.region_action", [("", "workflow"), ("", "copy")], tr_prefix="settings.region_action"),
            )
        )
        column.addWidget(
            self._row(
                "region", "settings.scroll_mode", "settings.scroll_mode.desc",
                self._combo("capture.scroll_mode", [("", "auto"), ("", "manual")], tr_prefix="settings.scroll_mode"),
            )
        )
        column.addWidget(
            self._row(
                "copy", "settings.multi_clipboard", "settings.multi_clipboard.desc",
                self._combo(
                    "capture.multi_clipboard", [("", "sequence"), ("", "combined")], tr_prefix="settings.multi_clipboard"
                ),
            )
        )
        column.addWidget(
            self._row("check", "settings.action_bar", "settings.action_bar.desc", self._toggle("selector.action_bar"))
        )

        name_card = Card(padding=16, spacing=8)
        head = SettingRow("edit")
        head.setProperty("card", False)
        head.layout().setContentsMargins(0, 0, 0, 0)
        self.rt.bind(head.title.setText, "settings.filename_template")
        self.rt.bind(head.description.setText, "settings.filename_template.desc")
        name_card.body.addWidget(head)
        self.template = QLineEdit(self.settings.get("capture.filename_template"))
        self.template.textChanged.connect(self._on_template)
        name_card.body.addWidget(self.template)
        tokens = label(role="caption", wrap=True)
        self.rt.bind(tokens.setText, "settings.filename_tokens", tokens=FILENAME_TOKENS)
        name_card.body.addWidget(tokens)
        self.preview = label(role="secondary")
        name_card.body.addWidget(self.preview)
        column.addWidget(name_card)

        folder_card = Card(padding=16, spacing=8)
        head = SettingRow("folder")
        head.setProperty("card", False)
        head.layout().setContentsMargins(0, 0, 0, 0)
        self.rt.bind(head.title.setText, "settings.output_dir")
        self.rt.bind(head.description.setText, "settings.output_dir.desc")
        folder_card.body.addWidget(head)
        row = QHBoxLayout()
        self.output_dir = QLineEdit(self.settings.get("capture.output_dir"))
        self.rt.bind(
            self.output_dir.setPlaceholderText, "settings.default_location", path=str(self.app.paths.captures_dir)
        )
        self.output_dir.editingFinished.connect(lambda: self._set("capture.output_dir", self.output_dir.text().strip()))
        browse = QPushButton()
        self.rt.bind(browse.setText, "settings.browse")
        browse.clicked.connect(self._browse)
        row.addWidget(self.output_dir, 1)
        row.addWidget(browse)
        self._lock(self.output_dir, "capture.output_dir")
        self._lock(browse, "capture.output_dir")
        folder_card.body.addLayout(row)
        column.addWidget(folder_card)

        column.addWidget(
            self._row("cursor", "settings.include_cursor", "settings.include_cursor.desc", self._toggle("capture.include_cursor"))
        )

        wm_title = label(role="subtitle")
        self.rt.bind(wm_title.setText, "settings.watermark")
        column.addSpacing(8)
        column.addWidget(wm_title)
        column.addWidget(self._row("shield", "settings.watermark.enabled", None, self._toggle("watermark.enabled")))
        wm_card = Card(padding=16, spacing=8)
        wm_label = label()
        self.rt.bind(wm_label.setText, "settings.watermark.text")
        wm_card.body.addWidget(wm_label)
        self.wm_text = QLineEdit(self.settings.get("watermark.text"))
        self.wm_text.editingFinished.connect(lambda: self._set("watermark.text", self.wm_text.text()))
        self._lock(self.wm_text, "watermark.text")
        wm_card.body.addWidget(self.wm_text)
        wm_tokens = label(role="caption")
        self.rt.bind(wm_tokens.setText, "settings.watermark.tokens")
        wm_card.body.addWidget(wm_tokens)
        wm_row = QHBoxLayout()
        pos_label = label(role="secondary")
        self.rt.bind(pos_label.setText, "settings.watermark.position")
        self.wm_position = QComboBox()
        from screenpilot.editor.watermark import POSITIONS

        for pos in POSITIONS:
            self.wm_position.addItem("", pos)
        self._retranslate_wm()
        self.rt.on_change(self._retranslate_wm)
        self.wm_position.setCurrentIndex(max(0, self.wm_position.findData(self.settings.get("watermark.position"))))
        self.wm_position.currentIndexChanged.connect(lambda _i: self._set("watermark.position", self.wm_position.currentData()))
        op_label = label(role="secondary")
        self.rt.bind(op_label.setText, "settings.watermark.opacity")
        self.wm_opacity = QSpinBox()
        self.wm_opacity.setRange(5, 100)
        self.wm_opacity.setSuffix(" %")
        self.wm_opacity.setValue(self.settings.get("watermark.opacity"))
        self.wm_opacity.valueChanged.connect(lambda v: self._set("watermark.opacity", v))
        for w in (pos_label, self.wm_position, op_label, self.wm_opacity):
            wm_row.addWidget(w)
        wm_row.addStretch(1)
        self._lock(self.wm_position, "watermark.position")
        self._lock(self.wm_opacity, "watermark.opacity")
        wm_card.body.addLayout(wm_row)
        column.addWidget(wm_card)

        ocr_title = label(role="subtitle")
        self.rt.bind(ocr_title.setText, "settings.ocr")
        column.addSpacing(8)
        column.addWidget(ocr_title)
        self.ocr_language = QComboBox()
        for code in ("auto", "en", "ro", "de"):
            self.ocr_language.addItem("", code)
        self._retranslate_ocr_combo()
        self.rt.on_change(self._retranslate_ocr_combo)
        self.ocr_language.setCurrentIndex(max(0, self.ocr_language.findData(self.settings.get("ocr.language"))))
        self.ocr_language.currentIndexChanged.connect(lambda _i: self._set("ocr.language", self.ocr_language.currentData()))
        column.addWidget(self._row("ocr", "settings.ocr.language", "settings.ocr.language.desc", self.ocr_language))
        column.addWidget(self._row("copy", "settings.ocr.auto_copy", None, self._toggle("ocr.auto_copy")))
        column.addWidget(self._row("text", "settings.ocr.show_window", None, self._toggle("ocr.show_window")))
        column.addStretch(1)

        self._on_format()
        self._update_preview()
        self.rt.on_change(self._update_preview)
        return page

    def _retranslate_wm(self) -> None:
        for i in range(self.wm_position.count()):
            self.wm_position.setItemText(i, self.t(f"wm.{self.wm_position.itemData(i)}"))

    def _retranslate_ocr_combo(self) -> None:
        names = {"en": "English", "ro": "Română", "de": "Deutsch"}
        for i in range(self.ocr_language.count()):
            code = self.ocr_language.itemData(i)
            self.ocr_language.setItemText(i, self.t("ocr.language.auto") if code == "auto" else names[code])

    def _on_format(self, *_):
        fmt = self.image_format.currentData()
        self.jpeg_quality.setEnabled(fmt == "jpg")
        if fmt != self.settings.get("capture.image_format"):
            self._set("capture.image_format", fmt)
        self._update_preview()

    def _on_template(self, text: str) -> None:
        if text.strip():
            self._set("capture.filename_template", text)
        self._update_preview()

    def _update_preview(self) -> None:
        from screenpilot.capture import FakeBackend, Monitor, Rect

        sample = FakeBackend([Monitor(1, Rect(0, 0, 4, 4), Rect(0, 0, 4, 4), True)]).grab(Rect(0, 0, 4, 4))
        result = CaptureResult(sample, "region", Rect(0, 0, 1280, 720), metadata={"window_title": "Report.docx - Word"})
        name = render_filename(self.template.text() or "capture", result)
        self.preview.setText(self.t("settings.filename_preview", name=f"{name}.{self.image_format.currentData()}"))

    def _browse(self) -> None:
        start = self.output_dir.text() or str(self.app.paths.captures_dir)
        folder = QFileDialog.getExistingDirectory(self, self.t("settings.output_dir"), start)
        if folder:
            self.output_dir.setText(folder)
            self._set("capture.output_dir", folder)

    # -- after capture --------------------------------------------------------
    def _build_after_capture(self) -> QWidget:
        page = Card(padding=16, spacing=10)
        layout = page.body
        hint = self._label("settings.after_capture.hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.actions_list = QListWidget()
        self.actions_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        configured = self.settings.get("workflow.after_capture")
        enabled = [s["id"] for s in configured if isinstance(s, dict)]
        ordered = [a for a in enabled if a in UI_WORKFLOW_ACTIONS] + [a for a in UI_WORKFLOW_ACTIONS if a not in enabled]
        for action_id in ordered:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, action_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setCheckState(Qt.CheckState.Checked if action_id in enabled else Qt.CheckState.Unchecked)
            self.actions_list.addItem(item)
        self._retranslate_actions()
        self.rt.on_change(self._retranslate_actions)
        self.actions_list.itemChanged.connect(lambda _i: self._save_actions())
        self.actions_list.model().rowsMoved.connect(lambda *_: self._save_actions())
        layout.addWidget(self.actions_list)

        stop_row = QHBoxLayout()
        stop_label = label()
        self.rt.bind(stop_label.setText, "settings.stop_on_error")
        stop_row.addWidget(stop_label, 1)
        stop_row.addWidget(self._toggle("workflow.stop_on_error"))
        layout.addLayout(stop_row)
        return page

    def _retranslate_actions(self) -> None:
        for row in range(self.actions_list.count()):
            item = self.actions_list.item(row)
            item.setText(self.t(f"workflow.action.{item.data(Qt.ItemDataRole.UserRole)}"))

    def _save_actions(self) -> None:
        previous = self.settings.get("workflow.after_capture")
        # Keep steps the UI does not manage (e.g. run_script with parameters) in place at the end.
        extra = [s for s in previous if isinstance(s, dict) and s.get("id") not in UI_WORKFLOW_ACTIONS]
        params = {s["id"]: s for s in previous if isinstance(s, dict) and s.get("id") in UI_WORKFLOW_ACTIONS}
        steps = []
        for row in range(self.actions_list.count()):
            item = self.actions_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                action_id = item.data(Qt.ItemDataRole.UserRole)
                steps.append(params.get(action_id, {"id": action_id}))
        self._set("workflow.after_capture", steps + extra)

    # -- hotkeys --------------------------------------------------------------
    def _build_hotkeys(self) -> QWidget:
        page = Card(padding=16, spacing=10)
        layout = page.body
        self.hotkey_search = QLineEdit()
        self.hotkey_search.setClearButtonEnabled(True)
        self.rt.bind(self.hotkey_search.setPlaceholderText, "hotkeys.search")
        self.hotkey_search.textChanged.connect(lambda _t: self._fill_hotkeys())
        layout.addWidget(self.hotkey_search)

        self.hotkey_table = QTableWidget(0, 3)
        self.hotkey_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.hotkey_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.hotkey_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hotkey_table.verticalHeader().setVisible(False)
        self.hotkey_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.hotkey_table.itemSelectionChanged.connect(self._on_hotkey_selected)
        layout.addWidget(self.hotkey_table)

        layout.addWidget(self._label("hotkeys.edit_hint"))
        edit_row = QHBoxLayout()
        self.key_edit = QKeySequenceEdit()
        self.key_edit.setMaximumSequenceLength(1)
        self.key_edit.editingFinished.connect(self._on_key_edited)
        inner = self.key_edit.findChild(QLineEdit)
        if inner is not None:  # Qt's own placeholder is English-only
            self.rt.bind(inner.setPlaceholderText, "hotkeys.press_keys")
        clear = QPushButton()
        self.rt.bind(clear.setText, "hotkeys.clear")
        clear.clicked.connect(self._clear_hotkey)
        edit_row.addWidget(self.key_edit, 1)
        edit_row.addWidget(clear)
        layout.addLayout(edit_row)

        self.conflicts = QLabel()
        self.conflicts.setWordWrap(True)
        layout.addWidget(self.conflicts)

        buttons = QHBoxLayout()
        for key, slot in (
            ("hotkeys.import", self._import_profile),
            ("hotkeys.export", self._export_profile),
            ("hotkeys.reset", self._reset_hotkeys),
        ):
            button = QPushButton()
            self.rt.bind(button.setText, key)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._fill_hotkeys()
        self.rt.on_change(self._fill_hotkeys)
        return page

    # -- capture modes ----------------------------------------------------------
    def _build_modes(self) -> QWidget:
        from screenpilot.capture.profiles import MODES

        page, column = self._column()
        intro = label(role="secondary", wrap=True)
        self.rt.bind(intro.setText, "modes.intro")
        column.addWidget(intro)
        card = Card(padding=12, spacing=4)
        self.mode_rows: dict[str, tuple[QLabel, QPushButton]] = {}
        for mode in MODES:
            row = QHBoxLayout()
            name = label()
            self.rt.bind(name.setText, mode.label_key)
            keys = label(role="kbd")
            swatch = QPushButton()
            swatch.setFixedSize(120, 30)
            swatch.setCursor(Qt.CursorShape.PointingHandCursor)
            self.rt.bind(swatch.setToolTip, "modes.pick")
            swatch.clicked.connect(lambda _=False, m=mode.id: self._pick_mode_color(m))
            row.addWidget(name, 1)
            row.addWidget(keys)
            row.addSpacing(12)
            row.addWidget(swatch)
            card.body.addLayout(row)
            self.mode_rows[mode.id] = (keys, swatch)
        hint = label(role="caption")
        self.rt.bind(hint.setText, "modes.shortcut_hint")
        card.body.addWidget(hint)
        column.addWidget(card)

        self.thickness_spin = QSpinBox()
        self.thickness_spin.setRange(1, 6)
        self.thickness_spin.setSuffix(" px")
        self.thickness_spin.setValue(self.settings.get("selector.thickness"))
        self.thickness_spin.valueChanged.connect(lambda v: self._set("selector.thickness", v))
        column.addWidget(self._row("region", "modes.thickness", None, self.thickness_spin))
        self.dim_spin = QSpinBox()
        self.dim_spin.setRange(0, 80)
        self.dim_spin.setSingleStep(5)
        self.dim_spin.setSuffix(" %")
        self.dim_spin.setValue(self.settings.get("selector.dim"))
        self.dim_spin.valueChanged.connect(lambda v: self._set("selector.dim", v))
        column.addWidget(self._row("theme", "modes.dim", None, self.dim_spin))
        column.addWidget(self._row("search", "modes.magnifier", "modes.magnifier.desc", self._toggle("selector.magnifier")))
        reset = QPushButton()
        self.rt.bind(reset.setText, "modes.reset")
        reset.clicked.connect(self._reset_mode_colors)
        column.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)
        column.addStretch(1)
        self._refresh_modes()
        self.rt.on_change(self._refresh_modes)
        self.controller.hotkeys_changed.connect(self._refresh_modes)
        return page

    def _refresh_modes(self) -> None:
        from screenpilot.capture.profiles import mode_color
        from screenpilot.ui.region_selector import selector_accent

        for mode_id, (keys, swatch) in self.mode_rows.items():
            hotkey = self.app.hotkeys.binding(mode_id)
            keys.setText(str(hotkey) if hotkey else "—")
            configured = mode_color(self.settings, mode_id)
            color = selector_accent(configured)
            text_color = "#000000" if color.lightness() > 150 else "#ffffff"
            swatch.setText(color.name().upper() if configured else self.t("modes.auto"))
            swatch.setStyleSheet(
                f"QPushButton {{ background: {color.name()}; color: {text_color}; border: 1px solid rgba(0,0,0,40);"
                " border-radius: 6px; font-weight: 600; }"
            )

    def _pick_mode_color(self, mode_id: str) -> None:
        from PySide6.QtWidgets import QColorDialog

        from screenpilot.capture.profiles import mode_color, set_mode_color
        from screenpilot.ui.region_selector import selector_accent

        current = selector_accent(mode_color(self.settings, mode_id))
        color = QColorDialog.getColor(current, self, self.t("modes.pick"))
        if color.isValid():
            set_mode_color(self.settings, mode_id, color.name())
            self._set("selector.colors", self.settings.get("selector.colors"))
            self._refresh_modes()

    def _reset_mode_colors(self) -> None:
        self._set("selector.colors", {})
        self._refresh_modes()

    # -- diagnostics ------------------------------------------------------------
    # -- recording ------------------------------------------------------------
    QUALITY_PRESETS = (("high", 17), ("balanced", 20), ("small", 26))

    def _combo(self, key: str, items: list[tuple[str, object]], *, tr_prefix: str | None = None) -> QComboBox:
        combo = QComboBox()
        combo.setMinimumWidth(180)
        for text, data in items:
            combo.addItem(text, data)

        def retranslate() -> None:
            if tr_prefix:
                for i in range(combo.count()):
                    combo.setItemText(i, self.t(f"{tr_prefix}.{combo.itemData(i)}"))

        retranslate()
        self.rt.on_change(retranslate)
        index = combo.findData(self.settings.get(key))
        combo.setCurrentIndex(max(0, index))
        combo.currentIndexChanged.connect(lambda _i: self._set(key, combo.currentData()))
        return self._lock(combo, key)

    def _spin(self, key: str, low: int, high: int, suffix_key: str | None = None, step: int = 1) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setValue(int(self.settings.get(key)))
        if suffix_key:
            self.rt.bind(spin.setSuffix, suffix_key)
        spin.valueChanged.connect(lambda v: self._set(key, v))
        return self._lock(spin, key)

    def _path_card(self, icon_name: str, title_key: str, desc_key: str, key: str, placeholder: str, *, pick_file: bool) -> tuple[Card, QLineEdit]:
        card = Card(padding=16, spacing=8)
        head = SettingRow(icon_name)
        head.setProperty("card", False)
        head.layout().setContentsMargins(0, 0, 0, 0)
        self.rt.bind(head.title.setText, title_key)
        self.rt.bind(head.description.setText, desc_key)
        card.body.addWidget(head)
        row = QHBoxLayout()
        edit = QLineEdit(self.settings.get(key) or "")
        edit.setPlaceholderText(placeholder)
        edit.editingFinished.connect(lambda: self._set(key, edit.text().strip()))
        browse = QPushButton()
        self.rt.bind(browse.setText, "settings.browse")

        def choose() -> None:
            if pick_file:
                path, _ = QFileDialog.getOpenFileName(self, self.t(title_key), edit.text() or "", "ffmpeg (ffmpeg.exe ffmpeg);;*.*")
            else:
                path = QFileDialog.getExistingDirectory(self, self.t(title_key), edit.text() or placeholder)
            if path:
                edit.setText(path)
                self._set(key, path)

        browse.clicked.connect(choose)
        row.addWidget(edit, 1)
        row.addWidget(browse)
        self._lock(edit, key)
        self._lock(browse, key)
        card.body.addLayout(row)
        return card, edit

    def _build_recording(self) -> QWidget:
        page, column = self._column()
        service = self.app.recording

        column.addWidget(
            self._row("video", "settings.rec.format", "settings.rec.format.desc",
                      self._combo("recording.format", [("", "mp4"), ("", "gif")], tr_prefix="settings.rec.format"))
        )
        column.addWidget(self._row("timer", "settings.rec.fps", None, self._spin("recording.fps", 5, 60, "settings.rec.fps_suffix")))
        self.rec_quality = QComboBox()
        for name, value in self.QUALITY_PRESETS:
            self.rec_quality.addItem("", value)

        def retranslate_quality() -> None:
            for i, (name, _v) in enumerate(self.QUALITY_PRESETS):
                self.rec_quality.setItemText(i, self.t(f"settings.rec.quality.{name}"))

        retranslate_quality()
        self.rt.on_change(retranslate_quality)
        current = int(self.settings.get("recording.quality"))
        self.rec_quality.setCurrentIndex(min(range(3), key=lambda i: abs(self.QUALITY_PRESETS[i][1] - current)))
        self.rec_quality.currentIndexChanged.connect(lambda _i: self._set("recording.quality", self.rec_quality.currentData()))
        self._lock(self.rec_quality, "recording.quality")
        column.addWidget(self._row("image", "settings.rec.quality", "settings.rec.quality.desc", self.rec_quality))
        column.addWidget(
            self._row("video", "settings.rec.engine", "settings.rec.engine.desc",
                      self._combo("recording.engine", [("", "auto"), ("", "windows"), ("", "ffmpeg")], tr_prefix="settings.rec.engine"))
        )
        self.windows_status = label(role="caption", wrap=True)
        column.addWidget(self.windows_status)
        self._refresh_windows_status()
        column.addWidget(
            self._row("sparkle", "settings.rec.encoder", "settings.rec.encoder.desc",
                      self._combo("recording.encoder", [("", "auto"), ("", "hardware"), ("", "software")], tr_prefix="settings.rec.encoder"))
        )

        audio_title = label(role="subtitle")
        self.rt.bind(audio_title.setText, "settings.rec.audio")
        column.addSpacing(8)
        column.addWidget(audio_title)
        audio_allowed = self.app.policy.allows("record_audio")
        self.rec_mic = QComboBox()
        self.rec_system = QComboBox()
        for combo, key in ((self.rec_mic, "recording.microphone"), (self.rec_system, "recording.system_audio")):
            combo.setMinimumWidth(240)
            combo.addItem(self.t("settings.rec.audio.none"), "")
            saved = self.settings.get(key) or ""
            if saved:
                combo.addItem(saved, saved)
                combo.setCurrentIndex(1)
            combo.currentIndexChanged.connect(lambda _i, c=combo, k=key: self._set(k, c.currentData()))
            self._lock(combo, key)
            if not audio_allowed:
                combo.setEnabled(False)
                combo.setToolTip(self.t("policy.feature_disabled"))
        column.addWidget(self._row("mic", "settings.rec.microphone", None, self.rec_mic))
        column.addWidget(self._row("speaker", "settings.rec.system_audio", "settings.rec.system_audio.desc", self.rec_system))
        detect = QPushButton()
        self.rt.bind(detect.setText, "settings.rec.detect_devices")
        detect.clicked.connect(self._detect_audio_devices)
        detect.setEnabled(audio_allowed)
        column.addWidget(self._row("search", "settings.rec.devices", "settings.rec.devices.desc", detect))

        fx_title = label(role="subtitle")
        self.rt.bind(fx_title.setText, "settings.rec.effects")
        column.addSpacing(8)
        column.addWidget(fx_title)
        column.addWidget(self._row("cursor", "settings.rec.show_cursor", None, self._toggle("recording.show_cursor")))
        column.addWidget(self._row("spotlight", "settings.rec.highlight", "settings.rec.highlight.desc", self._toggle("recording.highlight_cursor")))
        column.addWidget(self._row("select", "settings.rec.clicks", "settings.rec.clicks.desc", self._toggle("recording.click_effects")))
        column.addWidget(self._row("timer", "settings.rec.countdown", None, self._spin("recording.countdown", 0, 10, "settings.seconds_suffix")))
        column.addWidget(self._row("history", "settings.rec.max_minutes", None, self._spin("recording.max_minutes", 1, 240, "settings.rec.minutes_suffix")))
        column.addWidget(self._row("pause", "settings.rec.esc_pauses", "settings.rec.esc_pauses.desc", self._toggle("recording.esc_pauses")))

        ink_title = label(role="subtitle")
        self.rt.bind(ink_title.setText, "settings.rec.ink")
        column.addSpacing(8)
        column.addWidget(ink_title)
        from screenpilot.editor.document import COLORS

        ink_color = self._combo(
            "recording.ink_color", [(name, value) for name, value in COLORS.items() if name != "white"],
        )
        from PySide6.QtGui import QColor as _QColor, QIcon as _QIcon, QPixmap as _QPixmap

        for i, name in enumerate(n for n in COLORS if n != "white"):
            swatch = _QPixmap(14, 14)
            swatch.fill(_QColor(COLORS[name]))
            ink_color.setItemIcon(i, _QIcon(swatch))
            self.rt.bind(lambda text, i=i: ink_color.setItemText(i, text), f"settings.color.{name}")
        column.addWidget(self._row("pen", "settings.rec.ink_color", "settings.rec.ink.desc", ink_color))
        column.addWidget(
            self._row(
                "timer", "settings.rec.ink_fade", "settings.rec.ink_fade.desc",
                self._combo("recording.ink_fade_seconds", [("", v) for v in (0, 3, 5, 10, 30)], tr_prefix="settings.rec.ink_fade"),
            )
        )

        gif_title = label(role="subtitle")
        self.rt.bind(gif_title.setText, "settings.rec.gif")
        column.addSpacing(8)
        column.addWidget(gif_title)
        column.addWidget(self._row("film", "settings.rec.gif_fps", None, self._spin("recording.gif_fps", 5, 30, "settings.rec.fps_suffix")))
        column.addWidget(self._row("image", "settings.rec.gif_width", None, self._spin("recording.gif_max_width", 320, 3840, "settings.rec.px_suffix", step=80)))

        column.addSpacing(8)
        folder_card, _ = self._path_card(
            "folder", "settings.rec.output_dir", "settings.rec.output_dir.desc", "recording.output_dir",
            str(service.default_dir()), pick_file=False,
        )
        column.addWidget(folder_card)
        column.addWidget(self._row("folder", "settings.rec.open_folder", None, self._toggle("recording.open_folder_after")))
        ffmpeg_card, self.ffmpeg_edit = self._path_card(
            "settings", "settings.rec.ffmpeg", "settings.rec.ffmpeg.desc", "recording.ffmpeg_path", "ffmpeg.exe", pick_file=True,
        )
        self.ffmpeg_status = label(role="caption", wrap=True)
        ffmpeg_card.body.addWidget(self.ffmpeg_status)
        buttons = QHBoxLayout()
        check = QPushButton()
        self.rt.bind(check.setText, "settings.rec.ffmpeg.check")
        check.clicked.connect(lambda: self._refresh_ffmpeg_status(refresh=True))
        buttons.addWidget(check)
        copy_cmd = QPushButton()
        self.rt.bind(copy_cmd.setText, "settings.rec.ffmpeg.copy_command")
        self.rt.bind(copy_cmd.setToolTip, "settings.rec.ffmpeg.copy_command.tip", command=FFMPEG_INSTALL_COMMAND)
        copy_cmd.clicked.connect(self._copy_ffmpeg_command)
        buttons.addWidget(copy_cmd)
        buttons.addStretch(1)
        ffmpeg_card.body.addLayout(buttons)
        column.addWidget(ffmpeg_card)
        column.addWidget(
            self._row("video", "settings.rec.gif_notice", "settings.rec.gif_notice.desc", self._toggle("recording.gif_fallback_notice"))
        )
        column.addStretch(1)
        self.ffmpeg_edit.editingFinished.connect(lambda: self._refresh_ffmpeg_status(refresh=True))
        self._refresh_ffmpeg_status()
        self.rt.on_change(self._refresh_ffmpeg_status)
        return page

    def _refresh_windows_status(self) -> None:
        ok, reason = self.app.recording.windows_encoder()
        self.windows_status.setText(
            self.t("settings.rec.windows_status.ok") if ok else self.t("settings.rec.windows_status.missing", reason=reason)
        )

    def _refresh_ffmpeg_status(self, refresh: bool = False) -> None:
        info = self.app.recording.ffmpeg(refresh=refresh)
        if info is None:
            self.ffmpeg_status.setText(self.t("settings.rec.ffmpeg.missing"))
        else:
            self.ffmpeg_status.setText(self.t("settings.rec.ffmpeg.found", version=info.version, path=str(info.path)))

    def _copy_ffmpeg_command(self) -> None:
        self.controller.copy_text(FFMPEG_INSTALL_COMMAND)
        self.ffmpeg_status.setText(self.t("settings.rec.ffmpeg.command_copied", command=FFMPEG_INSTALL_COMMAND))

    def _detect_audio_devices(self) -> None:
        devices = self.app.recording.audio_devices()
        for combo in (self.rec_mic, self.rec_system):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(self.t("settings.rec.audio.none"), "")
            for name in devices:
                combo.addItem(name, name)
            if current and combo.findData(current) < 0:
                combo.addItem(current, current)
            combo.setCurrentIndex(max(0, combo.findData(current)))
            combo.blockSignals(False)
        if not devices:
            self.controller.notify.emit(self.t("settings.rec.devices"), self.t("settings.rec.devices.none"), False)

    def _build_diagnostics(self) -> QWidget:
        page, column = self._column()
        self._diag_page = None  # set by _page() wrapper below
        column.addWidget(
            self._row("edit", "diag.verbose", "diag.verbose.desc", self._toggle("diagnostics.verbose_logging"))
        )
        export = QPushButton()
        export.setProperty("accent", True)
        self.rt.bind(export.setText, "diag.export")
        export.clicked.connect(self.controller.export_diagnostics)
        column.addWidget(self._row("folder", "diag.export", "diag.export.desc", export))

        card = Card(padding=16, spacing=8)
        head = QHBoxLayout()
        title = label(role="subtitle")
        self.rt.bind(title.setText, "diag.timings")
        head.addWidget(title, 1)
        refresh = QPushButton()
        self.rt.bind(refresh.setText, "diag.refresh")
        refresh.clicked.connect(self._fill_timings)
        logs = QPushButton()
        self.rt.bind(logs.setText, "diag.open_logs")
        logs.clicked.connect(self.controller.open_log_folder)
        head.addWidget(refresh)
        head.addWidget(logs)
        card.body.addLayout(head)
        desc = label(role="caption")
        self.rt.bind(desc.setText, "diag.timings.desc")
        card.body.addWidget(desc)
        self.timing_table = QTableWidget(0, 6)
        self.timing_table.verticalHeader().setVisible(False)
        self.timing_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.timing_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.timing_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.timing_table.setMinimumHeight(220)
        card.body.addWidget(self.timing_table)
        self.timing_empty = label(role="secondary")
        self.rt.bind(self.timing_empty.setText, "diag.empty")
        card.body.addWidget(self.timing_empty)
        column.addWidget(card)
        column.addStretch(1)
        self._fill_timings()
        self.rt.on_change(self._fill_timings)
        self.nav.currentRowChanged.connect(
            lambda row: self._fill_timings() if self.stack.widget(row) is self._diag_page else None
        )
        return page

    def _fill_timings(self) -> None:
        summary = self.app.diagnostics.summary()
        self.timing_table.setHorizontalHeaderLabels(
            [self.t(f"diag.col.{c}") for c in ("operation", "count", "last", "median", "p95", "max")]
        )
        self.timing_table.setRowCount(len(summary))
        for row, (name, stats) in enumerate(summary.items()):
            key = f"diag.op.{name}"
            label_text = self.t(key) if self.app.translator.has(key) else name
            self.timing_table.setItem(row, 0, QTableWidgetItem(label_text))
            self.timing_table.setItem(row, 1, QTableWidgetItem(str(stats["count"])))
            for col, field in enumerate(("last", "median", "p95", "max"), start=2):
                item = QTableWidgetItem(f"{stats[field]:.0f} ms")
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.timing_table.setItem(row, col, item)
        self.timing_table.setVisible(bool(summary))
        self.timing_empty.setVisible(not summary)

    def _selected_action(self) -> str | None:
        rows = self.hotkey_table.selectionModel().selectedRows() if self.hotkey_table.selectionModel() else []
        if not rows:
            return None
        return self.hotkey_table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)

    def _fill_hotkeys(self) -> None:
        selected = self._selected_action()
        mgr = self.app.hotkeys
        self.hotkey_table.setHorizontalHeaderLabels(
            [self.t("hotkeys.col.action"), self.t("hotkeys.col.hotkey"), self.t("hotkeys.col.status")]
        )
        actions = mgr.search(self.hotkey_search.text(), label=lambda a: self.t(f"action.{a}"))
        status = mgr.diagnostics()
        self.hotkey_table.setRowCount(len(actions))
        for row, action in enumerate(actions):
            label = QTableWidgetItem(self.t(f"action.{action}"))
            label.setData(Qt.ItemDataRole.UserRole, action)
            hotkey = mgr.binding(action)
            st = status.get(action, RegistrationStatus.PENDING)
            if hotkey is None:
                st = RegistrationStatus.UNASSIGNED
            status_item = QTableWidgetItem(self.t(STATUS_KEYS[st]) if self.controller.hotkeys_active else "")
            if st is RegistrationStatus.FAILED:
                status_item.setForeground(Qt.GlobalColor.red)
            self.hotkey_table.setItem(row, 0, label)
            self.hotkey_table.setItem(row, 1, QTableWidgetItem(str(hotkey) if hotkey else "—"))
            self.hotkey_table.setItem(row, 2, status_item)
            if action == selected:
                self.hotkey_table.selectRow(row)
        messages = describe_conflicts(mgr.conflicts(), self.t)
        self.conflicts.setText("\n".join(messages) if messages else self.t("hotkeys.no_conflicts"))
        self.conflicts.setStyleSheet("color: #c42b1c;" if messages else "color: palette(mid);")

    def _on_hotkey_selected(self) -> None:
        action = self._selected_action()
        hotkey = self.app.hotkeys.binding(action) if action else None
        self.key_edit.blockSignals(True)
        self.key_edit.setKeySequence(QKeySequence(str(hotkey).replace("Win", "Meta")) if hotkey else QKeySequence())
        self.key_edit.blockSignals(False)

    def _on_key_edited(self) -> None:
        action = self._selected_action()
        if action is None:
            return
        try:
            hotkey = qt_sequence_to_hotkey(self.key_edit.keySequence())
        except HotkeyError:
            QMessageBox.warning(self, self.t("hotkeys.title"), self.t("hotkeys.invalid"))
            self._on_hotkey_selected()
            return
        self._apply_hotkey(action, hotkey)

    def _clear_hotkey(self) -> None:
        action = self._selected_action()
        if action is not None:
            self._apply_hotkey(action, None)
            self.key_edit.clear()

    def _apply_hotkey(self, action: str, hotkey: Hotkey | None) -> None:
        self.app.hotkeys.assign(action, hotkey)
        self._persist_hotkeys()

    def _persist_hotkeys(self) -> None:
        try:
            self.app.hotkeys.save(self.app.paths.hotkeys_file)
        except OSError:
            log.exception("Could not save hotkeys")
        self._fill_hotkeys()
        self.controller.hotkeys_changed.emit()

    def _import_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.t("hotkeys.import"), "", self.t("hotkeys.profile_filter"))
        if not path:
            return
        try:
            warnings = self.app.hotkeys.import_profile(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, HotkeyError) as exc:
            QMessageBox.warning(self, self.t("hotkeys.title"), f"{self.t('error.hotkey')}\n{exc}")
            return
        self._persist_hotkeys()
        if warnings:
            QMessageBox.information(
                self, self.t("hotkeys.title"), self.t("hotkeys.import_warnings", details="\n".join(warnings))
            )
        else:
            QMessageBox.information(self, self.t("hotkeys.title"), self.t("hotkeys.import_done"))

    def _export_profile(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, self.t("hotkeys.export"), "screenpilot-hotkeys.json", self.t("hotkeys.profile_filter")
        )
        if path:
            try:
                Path(path).write_text(json.dumps(self.app.hotkeys.export_profile(), indent=2), encoding="utf-8")
            except OSError as exc:
                QMessageBox.warning(self, self.t("hotkeys.title"), str(exc))

    def _reset_hotkeys(self) -> None:
        self.app.hotkeys.reset_to_defaults()
        self._persist_hotkeys()

