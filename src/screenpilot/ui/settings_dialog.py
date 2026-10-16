"""Settings window: general, capture, after-capture actions, hotkeys.

Every change is applied and saved immediately (no OK/Apply), and every label
follows the language live.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from screenpilot.capture.image import CaptureResult
from screenpilot.core.errors import HotkeyError
from screenpilot.hotkeys import Hotkey, RegistrationStatus, describe_conflicts
from screenpilot.ui.controller import AppController
from screenpilot.ui.qt_helpers import Retranslator, app_icon
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
        self.setMinimumSize(640, 520)
        self.rt.bind(self.setWindowTitle, "settings.title")

        self.tabs = QTabWidget(self)
        for key, builder in (
            ("settings.tab.general", self._build_general),
            ("settings.tab.capture", self._build_capture),
            ("settings.tab.after_capture", self._build_after_capture),
            ("settings.tab.hotkeys", self._build_hotkeys),
        ):
            page = builder()
            index = self.tabs.addTab(page, "")
            self.rt.bind(lambda text, i=index: self.tabs.setTabText(i, text), key)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.rt.bind(buttons.button(QDialogButtonBox.StandardButton.Close).setText, "settings.close")
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)

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
        page = QWidget()
        form = QFormLayout(page)
        self.language = QComboBox()
        for code, name in self.app.translator.available_languages().items():
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(self.app.translator.language)))
        self.language.currentIndexChanged.connect(
            lambda _i: self._set("general.language", self.language.currentData())
        )
        form.addRow(self._label("settings.language"), self.language)
        form.addRow("", self._label("settings.language.restart_not_needed"))

        for key, label_key in (
            ("general.start_minimized", "settings.start_minimized"),
            ("general.tester_mode", "settings.tester_mode"),
        ):
            box = QCheckBox()
            self.rt.bind(box.setText, label_key)
            box.setChecked(bool(self.settings.get(key)))
            box.toggled.connect(lambda checked, k=key: self._set(k, checked))
            form.addRow(box)

        note = self._label("settings.privacy_note")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid); margin-top: 16px;")
        form.addRow(note)
        return page

    # -- capture --------------------------------------------------------------
    def _build_capture(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.image_format = QComboBox()
        for fmt in IMAGE_FORMATS:
            self.image_format.addItem(fmt.upper(), fmt)
        self.image_format.setCurrentIndex(max(0, self.image_format.findData(self.settings.get("capture.image_format"))))
        self.image_format.currentIndexChanged.connect(self._on_format)
        form.addRow(self._label("settings.image_format"), self.image_format)

        self.jpeg_quality = QSpinBox()
        self.jpeg_quality.setRange(30, 100)
        self.jpeg_quality.setValue(self.settings.get("capture.jpeg_quality"))
        self.jpeg_quality.valueChanged.connect(lambda v: self._set("capture.jpeg_quality", v))
        form.addRow(self._label("settings.jpeg_quality"), self.jpeg_quality)

        self.delay = QSpinBox()
        self.delay.setRange(1, 60)
        self.delay.setValue(self.settings.get("capture.default_delay_seconds"))
        self.rt.bind(self.delay.setSuffix, "settings.seconds_suffix")
        self.delay.valueChanged.connect(lambda v: self._set("capture.default_delay_seconds", v))
        form.addRow(self._label("settings.default_delay"), self.delay)

        self.template = QLineEdit(self.settings.get("capture.filename_template"))
        self.template.textChanged.connect(self._on_template)
        form.addRow(self._label("settings.filename_template"), self.template)
        form.addRow("", self._label("settings.filename_tokens", tokens=FILENAME_TOKENS))
        self.preview = QLabel()
        form.addRow("", self.preview)

        row = QHBoxLayout()
        self.output_dir = QLineEdit(self.settings.get("capture.output_dir"))
        self.rt.bind(
            self.output_dir.setPlaceholderText, "settings.default_location", path=str(self.app.paths.captures_dir)
        )
        self.output_dir.editingFinished.connect(lambda: self._set("capture.output_dir", self.output_dir.text().strip()))
        browse = QPushButton()
        self.rt.bind(browse.setText, "settings.browse")
        browse.clicked.connect(self._browse)
        row.addWidget(self.output_dir)
        row.addWidget(browse)
        form.addRow(self._label("settings.output_dir"), row)

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
        page = QWidget()
        layout = QVBoxLayout(page)
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

        stop = QCheckBox()
        self.rt.bind(stop.setText, "settings.stop_on_error")
        stop.setChecked(bool(self.settings.get("workflow.stop_on_error")))
        stop.toggled.connect(lambda checked: self._set("workflow.stop_on_error", checked))
        layout.addWidget(stop)
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
        page = QWidget()
        layout = QVBoxLayout(page)
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

