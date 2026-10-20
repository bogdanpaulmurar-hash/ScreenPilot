"""The "Open editor" picker: choose what to edit when no capture was just taken.

Shown by the *Open editor* action (hub, tray › Tools, an optional hotkey) when
``editor.open_mode`` is ``"ask"``:

* **Last capture** — the newest one, also when it was only copied;
* the **10 most recent** saved captures as thumbnails;
* **From clipboard** — an image copied from any app;
* **Open file…** and **All captures…** (the history window, with search).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QPushButton, QToolButton, QVBoxLayout, QWidget

from screenpilot.capture.image import CaptureResult
from screenpilot.ui.qt_helpers import Retranslator, app_icon, qimage_from_captured, sharp_thumbnail
from screenpilot.ui.widgets import label

if TYPE_CHECKING:  # pragma: no cover
    from screenpilot.history import HistoryEntry
    from screenpilot.ui.controller import AppController

log = logging.getLogger(__name__)

RECENT = 10
COLUMNS = 5
THUMB = QSize(150, 92)
LAST_THUMB = QSize(320, 180)


class EditorPicker(QWidget):
    def __init__(self, controller: "AppController") -> None:
        super().__init__(None)
        self.controller = controller
        self.app = controller.app
        self.t = self.app.translator.t
        self.rt = rt = Retranslator(self.app.translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setProperty("page", True)
        self.setWindowIcon(app_icon())
        rt.bind(self.setWindowTitle, "editor.pick.title")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(12)
        title = label(role="title")
        rt.bind(title.setText, "editor.pick.title")
        root.addWidget(title)
        hint = label(role="caption", wrap=True)
        rt.bind(hint.setText, "editor.pick.hint")
        root.addWidget(hint)

        # Last capture: the big, default choice (Enter).
        self.last = self.controller.last_capture()
        self.last_button = QToolButton()
        self.last_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.last_button.setIconSize(LAST_THUMB)
        self.last_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.last_button.setMinimumWidth(LAST_THUMB.width() + 24)
        if self.last is not None:
            pixmap = QPixmap.fromImage(qimage_from_captured(self.last.image))
            self.last_button.setIcon(QIcon(sharp_thumbnail(pixmap, LAST_THUMB, self.devicePixelRatioF())))
            size = {"width": self.last.image.width, "height": self.last.image.height}
            rt.bind(self.last_button.setText, "editor.pick.last", **size)
            self.last_button.clicked.connect(lambda: self.choose(self.last))
        else:
            rt.bind(self.last_button.setText, "editor.pick.none")
            self.last_button.setEnabled(False)
        root.addWidget(self.last_button, 0, Qt.AlignmentFlag.AlignHCenter)

        recent_title = label(role="caption")
        rt.bind(recent_title.setText, "editor.pick.recent", count=RECENT)
        root.addWidget(recent_title)
        grid = QGridLayout()
        grid.setSpacing(8)
        self.recent_buttons: list[QToolButton] = []
        entries = self.app.history.entries()[:RECENT]
        for i, entry in enumerate(entries):
            button = self._entry_button(entry)
            grid.addWidget(button, i // COLUMNS, i % COLUMNS)
            self.recent_buttons.append(button)
        if not entries:
            empty = label(role="secondary")
            rt.bind(empty.setText, "editor.pick.no_recent")
            grid.addWidget(empty, 0, 0)
        root.addLayout(grid)

        actions = QHBoxLayout()
        self.clipboard_button = self._action_button("editor.pick.clipboard", self._from_clipboard)
        self.clipboard_button.setEnabled(not QGuiApplication.clipboard().image().isNull())
        self.file_button = self._action_button("editor.pick.file", self._from_file)
        self.all_button = self._action_button("editor.pick.all", self._show_all)
        for button in (self.clipboard_button, self.file_button, self.all_button):
            actions.addWidget(button)
        actions.addStretch(1)
        cancel = self._action_button("editor.pick.cancel", self.close)
        actions.addWidget(cancel)
        root.addLayout(actions)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.close)
        if self.last is not None:
            QShortcut(QKeySequence(Qt.Key.Key_Return), self, activated=lambda: self.choose(self.last))
        self.adjustSize()

    def _action_button(self, key: str, slot: Callable[[], object]) -> QPushButton:
        button = QPushButton()
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rt.bind(button.setText, key)
        button.clicked.connect(lambda _=False: slot())
        return button

    def _entry_button(self, entry: "HistoryEntry") -> QToolButton:
        button = QToolButton()
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setIconSize(THUMB)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if entry.thumbnail:
            button.setIcon(QIcon(QPixmap(entry.thumbnail)))
        when = entry.created.astimezone().strftime("%d.%m %H:%M")
        button.setText(f"{when} · {entry.width}×{entry.height}")
        button.setToolTip(entry.title or entry.path)
        button.clicked.connect(lambda _=False, e=entry: self._from_entry(e))
        return button

    # -- choices ------------------------------------------------------------------
    def choose(self, result: CaptureResult | None) -> QWidget | None:
        if result is None:
            return None
        self.close()
        return self.controller.open_editor(result)

    def _from_entry(self, entry: "HistoryEntry") -> None:
        self.choose(self.controller.load_history_capture(entry))

    def _from_clipboard(self) -> None:
        result = self.controller.clipboard_capture()
        if result is None:
            self.clipboard_button.setEnabled(False)
            return
        self.choose(result)

    def _from_file(self) -> None:
        self.choose(self.controller.open_image_file(self))

    def _show_all(self) -> None:
        self.close()
        self.controller.show_history()
