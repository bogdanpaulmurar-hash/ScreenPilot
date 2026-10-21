"""Dialog: choose which buttons the capture bar shows, and in which order."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from screenpilot.ui import icons
from screenpilot.ui.edge_items import DEFAULT_EDGE_BUTTONS, EDGE_ITEMS, SEPARATOR, edge_buttons
from screenpilot.ui.qt_helpers import Retranslator, app_icon
from screenpilot.ui.widgets import label, theme

ID_ROLE = Qt.ItemDataRole.UserRole


class EdgeBarEditor(QDialog):
    def __init__(self, app, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.t = app.translator.t
        self.rt = rt = Retranslator(app.translator)
        self.destroyed.connect(lambda *_: rt.dispose())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowIcon(app_icon())
        rt.bind(self.setWindowTitle, "edgebar.editor.title")
        self.resize(620, 460)

        self.available = QListWidget()
        self.chosen = QListWidget()
        for lst in (self.available, self.chosen):
            lst.setIconSize(QSize(20, 20))
            lst.setMinimumWidth(220)
        self.chosen.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.available.itemDoubleClicked.connect(lambda _i: self.add())
        self.chosen.itemDoubleClicked.connect(lambda _i: self.remove())

        middle = QVBoxLayout()
        middle.addStretch(1)
        self.buttons: dict[str, QPushButton] = {}
        for name, key, slot in (
            ("add", "edgebar.editor.add", self.add),
            ("remove", "edgebar.editor.remove", self.remove),
            ("up", "edgebar.editor.up", lambda: self.move(-1)),
            ("down", "edgebar.editor.down", lambda: self.move(1)),
            ("separator", "edgebar.editor.separator", self.add_separator),
        ):
            button = QPushButton()
            rt.bind(button.setText, key)
            button.clicked.connect(slot)
            middle.addWidget(button)
            self.buttons[name] = button
        middle.addStretch(1)

        left, right = QVBoxLayout(), QVBoxLayout()
        a_title, c_title = label(role="subtitle"), label(role="subtitle")
        rt.bind(a_title.setText, "edgebar.editor.available")
        rt.bind(c_title.setText, "edgebar.editor.chosen")
        left.addWidget(a_title)
        left.addWidget(self.available, 1)
        right.addWidget(c_title)
        right.addWidget(self.chosen, 1)
        lists = QHBoxLayout()
        lists.addLayout(left, 1)
        lists.addLayout(middle)
        lists.addLayout(right, 1)

        hint = label(role="caption", wrap=True)
        rt.bind(hint.setText, "edgebar.editor.hint")
        bottom = QHBoxLayout()
        reset = QPushButton()
        rt.bind(reset.setText, "edgebar.editor.reset")
        reset.clicked.connect(lambda: self.load(DEFAULT_EDGE_BUTTONS))
        bottom.addWidget(reset)
        bottom.addStretch(1)
        cancel = QPushButton()
        rt.bind(cancel.setText, "edgebar.editor.cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton()
        save.setProperty("accent", True)
        rt.bind(save.setText, "edgebar.editor.save")
        save.clicked.connect(self.save)
        bottom.addWidget(cancel)
        bottom.addWidget(save)
        self.buttons["save"] = save

        root = QVBoxLayout(self)
        root.addLayout(lists, 1)
        root.addWidget(hint)
        root.addLayout(bottom)
        self.load(edge_buttons(app.settings.get("general.edge_bar_buttons")))

    # -- items ----------------------------------------------------------------
    def _item(self, item_id: str) -> QListWidgetItem:
        if item_id == SEPARATOR:
            item = QListWidgetItem("—  " + self.t("edgebar.editor.separator_item") + "  —")
        else:
            spec = EDGE_ITEMS[item_id]
            text = self.t(spec.tooltip, seconds=self.app.settings.get("capture.default_delay_seconds"))
            item = QListWidgetItem(icons.icon(spec.icon, theme().text, 20), text)
        item.setData(ID_ROLE, item_id)
        return item

    def load(self, ids) -> None:
        ids = edge_buttons(list(ids))
        self.chosen.clear()
        self.available.clear()
        for item_id in ids:
            self.chosen.addItem(self._item(item_id))
        for item_id in EDGE_ITEMS:
            if item_id not in ids:
                self.available.addItem(self._item(item_id))

    def chosen_ids(self) -> list[str]:
        return [self.chosen.item(i).data(ID_ROLE) for i in range(self.chosen.count())]

    def add(self) -> None:
        row = self.available.currentRow()
        if row < 0:
            return
        item = self.available.takeItem(row)
        target = self.chosen.currentRow() + 1 if self.chosen.currentRow() >= 0 else self.chosen.count()
        self.chosen.insertItem(target, item)
        self.chosen.setCurrentRow(target)

    def remove(self) -> None:
        row = self.chosen.currentRow()
        if row < 0 or self.chosen.count() <= 1:
            return  # the bar keeps at least one button
        item = self.chosen.takeItem(row)
        if item.data(ID_ROLE) != SEPARATOR:
            self.available.addItem(item)

    def move(self, step: int) -> None:
        row = self.chosen.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.chosen.count():
            return
        item = self.chosen.takeItem(row)
        self.chosen.insertItem(target, item)
        self.chosen.setCurrentRow(target)

    def add_separator(self) -> None:
        row = self.chosen.currentRow()
        self.chosen.insertItem(row + 1 if row >= 0 else self.chosen.count(), self._item(SEPARATOR))

    def save(self) -> None:
        ids = edge_buttons(self.chosen_ids())
        value = [] if ids == DEFAULT_EDGE_BUTTONS else ids
        self.app.settings.set("general.edge_bar_buttons", value)
        try:
            self.app.settings.save()
        except OSError:
            pass
        self.accept()
