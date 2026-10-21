"""System tray icon and menu (the app's main entry point for the user)."""

from __future__ import annotations

from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu, QMessageBox, QSystemTrayIcon

from screenpilot import __version__
from screenpilot.capture.service import DELAY_PRESETS
from screenpilot.ui.controller import AppController
from screenpilot.ui.qt_helpers import app_icon

CAPTURE_ACTIONS = (
    "capture.region",
    "capture.window",
    "capture.fullscreen",
    "capture.all_monitors",
    "capture.repeat_last",
)
NOTIFY_MS = 3500


class TrayIcon(QSystemTrayIcon):
    def __init__(self, controller: AppController) -> None:
        super().__init__(app_icon())
        self.controller = controller
        self.app = controller.app
        rt = controller.retranslator

        menu = QMenu()
        self._menu = menu
        open_hub = QAction(menu)
        rt.bind(open_hub.setText, "tray.menu.open_hub")
        open_hub.triggered.connect(controller.show_hub)
        menu.addAction(open_hub)
        menu.setDefaultAction(open_hub)
        menu.addSeparator()
        for action_id in CAPTURE_ACTIONS:
            action = QAction(menu)
            rt.bind(action.setText, f"action.{action_id}")
            action.triggered.connect(lambda _=False, a=action_id: controller.trigger(a, from_menu=True))
            action.setData(action_id)
            menu.addAction(action)
        self._shortcut_actions = [a for a in menu.actions() if a.data()]

        delayed = QMenu(menu)
        rt.bind(delayed.setTitle, "tray.menu.delayed")
        for seconds in DELAY_PRESETS:
            action = QAction(delayed)
            rt.bind(action.setText, "tray.menu.delay_seconds", seconds=seconds)
            action.triggered.connect(lambda _=False, s=seconds: controller.capture_delayed(s))
            delayed.addAction(action)
        menu.addMenu(delayed)

        if self.app.policy.allows("recording"):
            menu.addSeparator()
            for action_id in ("record.video", "record.gif"):
                action = QAction(menu)
                action.triggered.connect(lambda _=False, a=action_id: controller.trigger(a, from_menu=True))
                action.setData(action_id)
                menu.addAction(action)
                self._shortcut_actions.append(action)
        self.stop_recording_action = QAction(menu)
        self.stop_recording_action.triggered.connect(controller.recording.stop)
        self.stop_recording_action.setVisible(False)
        menu.addAction(self.stop_recording_action)
        controller.recording.changed.connect(self._refresh_recording)

        tools = QMenu(menu)
        rt.bind(tools.setTitle, "tray.menu.tools")
        for action_id in ("procedure.toggle", "tools.color", "tools.measure", "capture.interval", "capture.edit", "capture.steps", "ocr.region", "app.open_editor", "app.edit_video"):
            action = QAction(tools)
            rt.bind(action.setText, f"action.{action_id}")
            action.triggered.connect(lambda _=False, a=action_id: controller.trigger(a, from_menu=True))
            action.setData(action_id)
            tools.addAction(action)
        menu.addMenu(tools)
        self._tool_actions = tools.actions()

        self.stop_interval_action = QAction(menu)
        self.stop_interval_action.triggered.connect(controller.stop_interval)
        self.stop_interval_action.setVisible(False)
        menu.addAction(self.stop_interval_action)
        controller.interval_changed.connect(self._refresh_interval)

        self.recent_menu = QMenu(menu)
        rt.bind(self.recent_menu.setTitle, "tray.menu.recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        menu.addMenu(self.recent_menu)
        menu.addSeparator()

        for key, slot in (
            ("tray.menu.history", controller.show_history),
            ("tray.menu.open_folder", controller.open_captures_folder),
        ):
            action = QAction(menu)
            rt.bind(action.setText, key)
            action.triggered.connect(slot)
            menu.addAction(action)
        menu.addSeparator()

        language_menu = QMenu(menu)
        rt.bind(language_menu.setTitle, "tray.menu.language")
        group = QActionGroup(language_menu)
        self._language_actions: dict[str, QAction] = {}
        for code, name in self.app.translator.available_languages().items():
            action = QAction(name, language_menu, checkable=True)
            action.setChecked(code == self.app.translator.language)
            action.triggered.connect(lambda _=False, c=code: self.app.settings.set("general.language", c))
            group.addAction(action)
            language_menu.addAction(action)
            self._language_actions[code] = action
        menu.addMenu(language_menu)

        for key, slot in (
            ("tray.menu.settings", lambda: controller.show_settings()),
            ("tray.menu.getting_started", controller.show_wizard),
            ("tray.menu.diagnostics", controller.export_diagnostics),
            ("tray.menu.about", self.show_about),
        ):
            action = QAction(menu)
            rt.bind(action.setText, key)
            action.triggered.connect(slot)
            menu.addAction(action)
        menu.addSeparator()
        exit_action = QAction(menu)
        rt.bind(exit_action.setText, "tray.menu.exit")
        exit_action.triggered.connect(controller.quit)
        menu.addAction(exit_action)

        self.setContextMenu(menu)
        rt.on_change(self._refresh)
        self._refresh()
        controller.notify.connect(self._on_notify)
        controller.notify_action.connect(self._on_notify_action)
        self._message_action = None
        self.messageClicked.connect(self._on_message_clicked)
        self.activated.connect(self._on_activated)
        self._unsub = self.app.settings.add_listener(self._on_setting)
        controller.hotkeys_changed.connect(self._refresh)

    def _refresh(self) -> None:
        """Shortcut hints in the menu and tooltip follow bindings and language."""
        t = self.app.translator.t
        for action in list(self._shortcut_actions) + list(getattr(self, "_tool_actions", [])):
            hotkey = self.app.hotkeys.binding(action.data())
            label = t(f"action.{action.data()}")
            action.setText(f"{label}\t{hotkey}" if hotkey else label)
        region = self.app.hotkeys.binding("capture.region")
        self.setToolTip(f'{t("tray.tooltip", hotkey=region or "-")}\nv{__version__}')
        for code, action in self._language_actions.items():
            action.setChecked(code == self.app.translator.language)

    def refresh(self) -> None:
        self._refresh()

    def _refresh_recording(self) -> None:
        recording = self.controller.recording.recording
        self.stop_recording_action.setVisible(recording)
        if recording:
            self.stop_recording_action.setText(self.app.translator.t("recording.stop"))

    def _refresh_interval(self) -> None:
        state = getattr(self.controller, "interval", None)
        self.stop_interval_action.setVisible(bool(state))
        if state:
            self.stop_interval_action.setText(self.app.translator.t("interval.stop", done=state["done"]))

    def _fill_recent(self) -> None:
        from PySide6.QtGui import QIcon, QPixmap

        self.recent_menu.clear()
        entries = self.app.history.entries()[:10]
        if not entries:
            empty = self.recent_menu.addAction(self.app.translator.t("tray.menu.recent.empty"))
            empty.setEnabled(False)
            return
        for entry in entries:
            when = entry.created.astimezone().strftime("%H:%M")
            action = self.recent_menu.addAction(f"{when}  ·  {entry.width}×{entry.height}  ·  {entry.title or entry.mode}")
            if entry.thumbnail:
                action.setIcon(QIcon(QPixmap(entry.thumbnail)))
            action.triggered.connect(lambda _=False, e=entry: self._copy_entry(e))

    def _copy_entry(self, entry) -> None:
        result = self.controller.load_history_capture(entry)
        if result is not None:
            self.controller.copy_result(result)

    def _on_setting(self, key: str, _value) -> None:
        if key == "general.language":
            self._refresh()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:  # single left click
            self.controller.trigger("capture.region", from_menu=True)
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.controller.show_hub()

    def _on_notify(self, title: str, body: str, is_error: bool) -> None:
        self._message_action = None
        if not self.isVisible():  # DevPilot owns the tray: balloons need a visible icon
            self._toast(title, body, is_error, None)
            return
        icon = QSystemTrayIcon.MessageIcon.Warning if is_error else QSystemTrayIcon.MessageIcon.Information
        self.showMessage(title, body, icon, NOTIFY_MS)

    def _on_notify_action(self, title: str, body: str, is_error: bool, action) -> None:
        """A message that does something when clicked (e.g. open the right settings page)."""
        if not self.isVisible():
            self._toast(title, body, is_error, action)
            return
        self._on_notify(title, body, is_error)
        self._message_action = action

    def _toast(self, title: str, body: str, is_error: bool, action) -> None:
        from screenpilot.ui.toast import Toast

        self.last_toast = Toast(title, body, error=is_error, action=action)
        self.last_toast.show_in_corner()

    def _on_message_clicked(self) -> None:
        action, self._message_action = self._message_action, None
        if action is not None:
            action()

    def show_about(self) -> None:
        t = self.app.translator.t
        QMessageBox.about(None, t("tray.menu.about"), t("about.body", version=__version__))
