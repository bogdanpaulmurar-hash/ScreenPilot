"""Capture Hub: the app's home window (Snagit-style all-in-one launcher).

Big capture-mode tiles, quick option chips (delay, copy, save, preview) and a
strip of recent captures. Everything a user needs in one glance; power users
keep using shortcuts.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from screenpilot.capture.service import DELAY_PRESETS
from screenpilot.core.events import Event
from screenpilot.history import EVENT_HISTORY_CHANGED, HistoryEntry
from screenpilot.ui.controller import AppController
from screenpilot.ui.qt_helpers import Retranslator, app_icon
from screenpilot.ui.widgets import Chip, IconBadge, IconButton, ModeTile, label, on_theme_changed

log = logging.getLogger(__name__)

HIDE_BEFORE_CAPTURE_MS = 220
RECENT_COUNT = 5
DELAY_CYCLE = (0, *DELAY_PRESETS)

TILES = (
    # icon, label key, action, soon
    ("region", "hub.mode.region", "capture.region", False),
    ("window", "hub.mode.window", "capture.window", False),
    ("monitor", "hub.mode.screen", "capture.fullscreen", False),
    ("monitors", "hub.mode.all", "capture.all_monitors", False),
    ("scroll", "hub.mode.scrolling", "capture.scrolling", False),
    ("procedure", "hub.mode.procedure", "procedure.toggle", False),
    ("video", "hub.mode.video", "record.video", False),
    ("film", "hub.mode.gif", "record.gif", False),
)


class CaptureHub(QWidget):
    def __init__(self, controller: AppController) -> None:
        super().__init__(None)
        self.controller = controller
        self.app = controller.app
        self.t = self.app.translator.t
        self.rt = rt = Retranslator(self.app.translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setObjectName("Page")
        self.setProperty("page", True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowIcon(app_icon())
        self.setWindowTitle("ScreenPilot")
        self.setMinimumWidth(640)
        self.delay = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(14)

        # Header
        header = QHBoxLayout()
        header.addWidget(IconBadge("logo", 28, token="accent"))
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("ScreenPilot", "title"))
        tagline = label(role="caption")
        rt.bind(tagline.setText, "app.tagline")
        titles.addWidget(tagline)
        header.addLayout(titles, 1)
        for icon_name, key, slot in (
            ("edit", "action.app.open_editor", controller.edit_last_capture),
            ("history", "tray.menu.history", controller.show_history),
            ("settings", "settings.title", lambda: controller.show_settings()),
        ):
            button = IconButton(icon_name, size=20)
            rt.bind(button.setToolTip, key)
            button.clicked.connect(slot)
            header.addWidget(button)
        root.addLayout(header)

        # Mode tiles
        section = label(role="caption")
        rt.bind(section.setText, "hub.capture_section")
        root.addWidget(section)
        grid = QGridLayout()
        grid.setSpacing(10)
        self.tiles: dict[str, ModeTile] = {}
        for i, (icon_name, key, action, soon) in enumerate(TILES):
            tile = ModeTile(icon_name, soon=soon)
            rt.bind(tile.setText, key)
            if soon:
                rt.bind(tile.set_shortcut_text, "hub.soon")
                rt.bind(tile.setToolTip, "hub.soon")
            else:
                tile.clicked.connect(lambda _=False, a=action: self.start_capture(a))
                self.tiles[action] = tile
            grid.addWidget(tile, i // 4, i % 4)
        root.addLayout(grid)

        # Option chips
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.delay_chip = Chip("timer")
        self.delay_chip.clicked.connect(self._cycle_delay)
        rt.bind(self.delay_chip.setToolTip, "hub.tip.delay")
        chips.addWidget(self.delay_chip)
        self.copy_chip = self._workflow_chip("copy", "hub.copy", "copy_image", "hub.tip.copy")
        self.save_chip = self._workflow_chip("save", "hub.save", "save", "hub.tip.save")
        chips.addWidget(self.copy_chip)
        chips.addWidget(self.save_chip)
        self.preview_chip = Chip("image")
        self.preview_chip.setCheckable(True)
        self.preview_chip.setChecked(bool(self.app.settings.get("capture.show_preview")))
        self.preview_chip.toggled.connect(lambda on: self._set("capture.show_preview", on))
        rt.bind(self.preview_chip.setText, "hub.preview")
        rt.bind(self.preview_chip.setToolTip, "hub.tip.preview")
        chips.addWidget(self.preview_chip)
        chips.addStretch(1)
        root.addLayout(chips)

        # Recent captures
        recent_header = QHBoxLayout()
        recent_label = label(role="caption")
        rt.bind(recent_label.setText, "hub.recent")
        recent_header.addWidget(recent_label)
        recent_header.addStretch(1)
        see_all = QPushButton()
        see_all.setProperty("flat", True)
        see_all.setCursor(Qt.CursorShape.PointingHandCursor)
        rt.bind(see_all.setText, "hub.see_all")
        see_all.clicked.connect(controller.show_history)
        recent_header.addWidget(see_all)
        root.addLayout(recent_header)
        self.recent_row = QHBoxLayout()
        self.recent_row.setSpacing(8)
        root.addLayout(self.recent_row)
        self.recent_empty = label(role="secondary")
        rt.bind(self.recent_empty.setText, "hub.recent_empty")
        root.addWidget(self.recent_empty)

        root.addStretch(1)
        footer = QHBoxLayout()
        footer.addWidget(IconBadge("shield", 14, token="success"))
        privacy = label(role="caption")
        rt.bind(privacy.setText, "hub.footer")
        footer.addWidget(privacy, 1)
        from screenpilot import __version__

        version = label(role="caption")
        version.setText(f"v{__version__}")
        version.setToolTip(f"ScreenPilot {__version__}")
        footer.addWidget(version)
        self.version_label = version
        root.addLayout(footer)

        rt.on_change(self._refresh)
        controller.bridge.event.connect(self._on_event)
        controller.hotkeys_changed.connect(self._refresh)
        controller.procedure_changed.connect(self._refresh)
        self._settings_unsub = self.app.settings.add_listener(lambda key, _v: self._on_setting(key))
        self.destroyed.connect(lambda *_: self._settings_unsub())
        on_theme_changed(self, lambda _t: self.update())
        self._refresh()

    # -- options --------------------------------------------------------------
    def _set(self, key: str, value) -> None:
        self.app.settings.set(key, value)
        try:
            self.app.settings.save()
        except OSError:
            log.exception("Could not save settings")

    def _workflow_chip(self, icon_name: str, text_key: str, action_id: str, tip_key: str) -> Chip:
        chip = Chip(icon_name)
        chip.setCheckable(True)
        chip.setProperty("action_id", action_id)
        self.rt.bind(chip.setText, text_key)
        self.rt.bind(chip.setToolTip, tip_key, params_fn=lambda: {"folder": str(self.app.output_dir)})
        chip.toggled.connect(lambda on, a=action_id: self._toggle_step(a, on))
        return chip

    def _enabled_steps(self) -> list[str]:
        return [s.get("id") for s in self.app.settings.get("workflow.after_capture") if isinstance(s, dict)]

    def _toggle_step(self, action_id: str, on: bool) -> None:
        steps = [s for s in self.app.settings.get("workflow.after_capture") if isinstance(s, dict)]
        present = any(s.get("id") == action_id for s in steps)
        if on and not present:
            # 'save' must run first so later steps (copy path, scripts) can use the file.
            steps = ([{"id": action_id}] + steps) if action_id == "save" else (steps + [{"id": action_id}])
        elif not on and present:
            steps = [s for s in steps if s.get("id") != action_id]
        else:
            return
        self._set("workflow.after_capture", steps)

    def _cycle_delay(self) -> None:
        index = DELAY_CYCLE.index(self.delay) if self.delay in DELAY_CYCLE else 0
        self.delay = DELAY_CYCLE[(index + 1) % len(DELAY_CYCLE)]
        self._refresh_delay()

    def _refresh_delay(self) -> None:
        self.delay_chip.setCheckable(True)
        self.delay_chip.setChecked(self.delay > 0)
        text = self.t("hub.delay", seconds=self.delay) if self.delay else self.t("hub.delay_off")
        self.delay_chip.setText(text)

    def _on_setting(self, key: str) -> None:
        if key in ("workflow.after_capture", "capture.show_preview"):
            self._refresh()

    # -- capture --------------------------------------------------------------
    def start_capture(self, action: str) -> None:
        """Hide the hub so it is not in the picture, then capture (optionally delayed)."""
        if not self.controller.allowed(action):
            self.controller.notify.emit(self.t("app.name"), self.t("policy.feature_disabled"), True)
            return
        self.hide()
        if action == "procedure.toggle":
            self.controller.toggle_procedure()
            return
        self.controller.hide_chrome()

        def go() -> None:
            handler = self.controller.actions[action]
            if self.delay:
                self.controller.capture_delayed(self.delay, then=handler)
            else:
                handler()

        QTimer.singleShot(HIDE_BEFORE_CAPTURE_MS, go)

    # -- state ----------------------------------------------------------------
    def _on_event(self, event: Event) -> None:
        if event.name == EVENT_HISTORY_CHANGED:
            self._refresh_recent()

    def _refresh(self) -> None:
        for action, tile in self.tiles.items():
            hotkey = self.app.hotkeys.binding(action)
            tile.set_shortcut_text(str(hotkey) if hotkey else "")
        session = self.controller.procedure
        if session is not None and "procedure.toggle" in self.tiles:
            self.tiles["procedure.toggle"].set_shortcut_text(self.t("hub.procedure.recording", count=len(session.steps)))
        steps = self._enabled_steps()
        for chip in (self.copy_chip, self.save_chip):
            chip.blockSignals(True)
            chip.setChecked(chip.property("action_id") in steps)
            chip.blockSignals(False)
        self.preview_chip.blockSignals(True)
        self.preview_chip.setChecked(bool(self.app.settings.get("capture.show_preview")))
        self.preview_chip.blockSignals(False)
        self._refresh_delay()
        self._refresh_recent()

    def _refresh_recent(self) -> None:
        while self.recent_row.count():
            item = self.recent_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        entries = self.app.history.entries()[:RECENT_COUNT]
        self.recent_empty.setVisible(not entries)
        for entry in entries:
            self.recent_row.addWidget(self._thumb_button(entry))
        if entries:
            self.recent_row.addStretch(1)

    def _thumb_button(self, entry: HistoryEntry) -> QToolButton:
        button = QToolButton()
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIconSize(QSize(80, 52))
        button.setFixedSize(88, 60)
        if entry.thumbnail and Path(entry.thumbnail).exists():
            button.setIcon(QIcon(QPixmap(entry.thumbnail)))
        button.setToolTip(f"{Path(entry.path).name}\n{entry.width}×{entry.height}")
        button.clicked.connect(lambda _=False, p=entry.path: self._open(p))
        return button

    def _open(self, path: str) -> None:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        else:
            self.app.shell.reveal(Path(path))
