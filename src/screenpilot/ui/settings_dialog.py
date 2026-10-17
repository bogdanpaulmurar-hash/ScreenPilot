"""Settings window: general, capture, after-capture actions, hotkeys.

Every change is applied and saved immediately (no OK/Apply), and every label
follows the language live.
"""

from __future__ import annotations

import json
import logging
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
        for icon_name, key, builder in (
            ("general", "settings.nav.general", self._build_general),
            ("region", "settings.nav.capture", self._build_capture),
            ("sparkle", "settings.nav.modes", self._build_modes),
            ("workflow", "settings.nav.after_capture", self._build_after_capture),
            ("keyboard", "settings.nav.hotkeys", self._build_hotkeys),
            ("settings", "settings.nav.diagnostics", self._build_diagnostics),
        ):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, icon_name)
            self.nav.addItem(item)
            self.rt.bind(item.setText, key)
            wrapped = self._page(key, builder())
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

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(side)
        layout.addWidget(self.stack, 1)

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
        return toggle

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
        folder_card.body.addLayout(row)
        column.addWidget(folder_card)
        column.addStretch(1)

        self._on_format()
        self._update_preview()
        self.rt.on_change(self._update_preview)
        return page

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
        self.timing_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
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

