"""Capture history window: thumbnails, search, period filter, quick actions.

Double-click (or Edit) opens a capture in the editor; "Open image…" edits any
picture on disk, so older captures that fell out of the history stay reachable.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from screenpilot.capture.image import CapturedImage
from screenpilot.core.events import Event
from screenpilot.history import EVENT_HISTORY_CHANGED, HistoryEntry
from screenpilot.ui import icons
from screenpilot.ui.controller import AppController
from screenpilot.ui.qt_helpers import Retranslator, app_icon
from screenpilot.ui.widgets import label, on_theme_changed, theme

log = logging.getLogger(__name__)

THUMB = QSize(220, 140)
PERIODS = (("all", None), ("today", 0), ("week", 7), ("month", 30))


class HistoryWindow(QWidget):
    def __init__(self, controller: AppController) -> None:
        super().__init__(None)
        self.controller = controller
        self.app = controller.app
        self.t = self.app.translator.t
        self.rt = rt = Retranslator(self.app.translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setProperty("page", True)
        self.setWindowIcon(app_icon())
        self.resize(980, 640)
        rt.bind(self.setWindowTitle, "history.title")

        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        rt.bind(self.search.setPlaceholderText, "history.search")
        self.search.textChanged.connect(lambda _t: self.refresh())

        self.period = QComboBox()
        for key, _days in PERIODS:
            self.period.addItem("", key)
        self.period.currentIndexChanged.connect(lambda _i: self.refresh())

        self.count = label(role="caption")
        open_file = QPushButton()
        rt.bind(open_file.setText, "history.open_file")
        rt.bind(open_file.setToolTip, "history.open_file.tip")
        open_file.clicked.connect(lambda: self.controller.edit_file(parent=self))
        self.open_file_button = open_file

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.period)
        top.addWidget(self.count)
        top.addWidget(open_file)

        self.list = QListWidget()
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setIconSize(THUMB)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setSpacing(8)
        self.list.setWordWrap(True)
        self.list.setUniformItemSizes(True)
        self.list.itemDoubleClicked.connect(lambda item: self._edit())
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)

        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)

        buttons = QHBoxLayout()
        self._buttons: dict[str, QPushButton] = {}
        for key, icon_name, slot, accent in (
            ("history.edit", "edit", self._edit, True),
            ("history.copy_image", "copy", self._copy_image, False),
            ("history.copy_path", None, self._copy_path, False),
            ("history.open", "image", lambda: self._open(self._current()), False),
            ("history.open_folder", "folder", self._reveal, False),
        ):
            button = QPushButton()
            button.setProperty("accent", accent)
            button.setProperty("icon_name", icon_name)
            rt.bind(button.setText, key)
            button.clicked.connect(slot)
            buttons.addWidget(button)
            self._buttons[key] = button
        buttons.addStretch(1)
        clear = QPushButton()
        rt.bind(clear.setText, "history.clear")
        clear.clicked.connect(self._clear)
        buttons.addWidget(clear)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty, 1)
        layout.addLayout(buttons)

        controller.bridge.event.connect(self._on_event)
        self.list.currentItemChanged.connect(lambda *_: self._update_buttons())
        rt.on_change(self._retranslate)
        on_theme_changed(self, self._restyle)
        self._retranslate()
        from screenpilot.ui.qt_helpers import on_escape

        on_escape(self, self.close)  # Esc closes the window

    def _restyle(self, t) -> None:
        for button in self._buttons.values():
            name = button.property("icon_name")
            if name:
                color = t.on_accent if button.property("accent") else t.text
                button.setIcon(icons.icon(name, color, 16))

    def _retranslate(self) -> None:
        for i, (key, _days) in enumerate(PERIODS):
            self.period.setItemText(i, self.t(f"history.period.{key}"))
        self.refresh()

    # -- data -----------------------------------------------------------------
    def _on_event(self, event: Event) -> None:
        if event.name == EVENT_HISTORY_CHANGED:
            self.refresh()

    def _filtered(self) -> list[HistoryEntry]:
        entries = self.app.history.search(self.search.text())
        days = dict(PERIODS).get(self.period.currentData())
        if days is None:
            return entries
        now = datetime.now(timezone.utc).astimezone()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
        return [e for e in entries if e.created.astimezone() >= start]

    def refresh(self) -> None:
        entries = self._filtered()
        self.list.clear()
        for entry in entries:
            when = entry.created.astimezone().strftime("%Y-%m-%d %H:%M")
            mode = self.t(f"capture.mode.{entry.mode}")
            item = QListWidgetItem(self.t("history.item", mode=mode, width=entry.width, height=entry.height, time=when))
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
            item.setToolTip(f"{entry.path}\n{self.t('history.double_click')}")
            if entry.thumbnail and Path(entry.thumbnail).exists():
                item.setIcon(QIcon(QPixmap(entry.thumbnail)))
            self.list.addItem(item)
        has_any = bool(entries)
        self.list.setVisible(has_any)
        self.empty.setVisible(not has_any)
        self.count.setText(self.t("history.count", count=len(entries), limit=self.app.history.limit))
        hotkey = self.app.hotkeys.binding("capture.region")
        self.empty.setText(self.t("history.empty", hotkey=hotkey or "—"))
        if has_any:
            self.list.setCurrentRow(0)
        self._update_buttons()

    def _entry(self, item: QListWidgetItem | None) -> HistoryEntry | None:
        if item is None:
            return None
        entry_id = item.data(Qt.ItemDataRole.UserRole)
        return next((e for e in self.app.history.entries() if e.id == entry_id), None)

    def _current(self) -> HistoryEntry | None:
        return self._entry(self.list.currentItem())

    def _update_buttons(self) -> None:
        enabled = self._current() is not None
        for button in self._buttons.values():
            button.setEnabled(enabled)

    # -- actions --------------------------------------------------------------
    def _copy_image(self) -> None:
        entry = self._current()
        if entry is None:
            return
        try:
            from PIL import Image

            with Image.open(entry.path) as img:
                self.app.clipboard.set_image(CapturedImage.from_pil(img))
        except Exception as exc:  # noqa: BLE001
            log.exception("Copy image failed")
            QMessageBox.warning(self, self.t("history.title"), str(exc))

    def _copy_path(self) -> None:
        entry = self._current()
        if entry is not None:
            try:
                self.app.clipboard.set_text(entry.path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, self.t("history.title"), str(exc))

    def _open(self, entry: HistoryEntry | None) -> None:
        if entry is None:
            return
        if sys.platform == "win32":
            os.startfile(entry.path)  # noqa: S606 - opens with the user's default image viewer
        else:
            self.app.shell.reveal(Path(entry.path))

    def _reveal(self) -> None:
        entry = self._current()
        if entry is not None:
            self.app.shell.reveal(Path(entry.path))

    def _edit(self) -> QWidget | None:
        entry = self._current()
        return self.controller.edit_history_entry(entry) if entry is not None else None

    def _steps(self) -> None:
        entry = self._current()
        if entry is None:
            return
        result = self.controller.load_history_capture(entry)
        if result is not None:
            self.controller.open_steps(result)

    def _remove(self) -> None:
        entry = self._current()
        if entry is not None:
            self.app.history.remove(entry.id)

    def _clear(self) -> None:
        answer = QMessageBox.question(self, self.t("history.clear"), self.t("history.confirm_clear"))
        if answer == QMessageBox.StandardButton.Yes:
            self.app.history.clear()

    def _context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        self.list.setCurrentItem(item)
        t = theme()
        menu = QMenu(self)
        for key, icon_name, slot in (
            ("history.edit", "edit", self._edit),
            ("history.open", "image", lambda: self._open(self._current())),
            ("history.copy_image", "copy", self._copy_image),
            ("history.copy_path", None, self._copy_path),
            ("history.open_folder", "folder", self._reveal),
            ("history.steps", "steps", self._steps),
            ("history.remove", "trash", self._remove),
        ):
            action = menu.addAction(self.t(key), slot)
            if icon_name:
                action.setIcon(icons.icon(icon_name, t.text, 16))
        menu.exec(self.list.viewport().mapToGlobal(pos))
