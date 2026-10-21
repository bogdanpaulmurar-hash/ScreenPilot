"""Desktop entry point: ``screenpilot-gui`` or ``python -m screenpilot gui``."""

from __future__ import annotations

import logging
import sys
from typing import Sequence

log = logging.getLogger(__name__)


def self_test() -> int:
    """Headless check used by the packaging pipeline: start the core, capture, export diagnostics."""
    import tempfile
    from pathlib import Path

    from screenpilot.app import create_app
    from screenpilot.core.paths import AppPaths

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(paths=AppPaths.under(Path(tmp), portable=True))
        try:
            result = app.capture.capture_all_monitors()
            steps, ctx = app.run_after_capture(result, [{"id": "save"}])
            ok = ctx.saved_path is not None and ctx.saved_path.exists()
            from PySide6.QtWidgets import QApplication

            qt = QApplication.instance() or QApplication([])
            from screenpilot.ui.controller import AppController

            controller = AppController(app, register_hotkeys=False)
            controller.show_hub().close()
            controller.shutdown()
            del qt
            Path(tmp, "self-test.ok").write_text("ok")
            report = Path(sys.executable).with_name("self-test.txt")
            try:
                report.write_text(f"ok={ok} size={result.image.width}x{result.image.height}\n", encoding="utf-8")
            except OSError:
                pass
            return 0 if ok else 1
        except Exception:  # noqa: BLE001
            log.exception("Self-test failed")
            return 2


def _option(args: list[str], name: str) -> str | None:
    if name in args:
        index = args.index(name)
        if index + 1 < len(args):
            return args[index + 1]
    return None


def startup_command(args: list[str]) -> dict:
    """What this start asks for: an action (``--action id [--seconds n] [--path p]``) or just the app."""
    from screenpilot.instance import action_message

    action = _option(args, "--action")
    if action:
        seconds = _option(args, "--seconds")
        return action_message(action, seconds=float(seconds) if seconds else None, path=_option(args, "--path"))
    if "--autostart" in args:
        return {"cmd": "ping"}  # started with Windows while already running: nothing to show
    return action_message("app.show_hub")


def main(argv: Sequence[str] | None = None) -> int:
    import time

    args = list(argv) if argv is not None else sys.argv
    if "--self-test" in args:
        return self_test()
    started = time.perf_counter()

    # Already running? Hand the request over (hub, region capture, …) and exit:
    # no second tray icon, no hotkey conflicts, no "already running" box.
    from screenpilot.core.paths import resolve_paths
    from screenpilot.instance import send

    phases: dict[str, float] = {}
    last = [started]

    def phase(name: str) -> None:
        """Duration of each start-up step (written to the timing journal: diagnostics)."""
        now = time.perf_counter()
        phases[name.split(".", 1)[1]] = round((now - last[0]) * 1000, 1)
        last[0] = now

    command = startup_command(args)
    data_dir = resolve_paths().ensure().data_dir
    if send(data_dir, command) is not None:
        log.info("ScreenPilot is already running: request forwarded (%s)", command.get("action", command["cmd"]))
        return 0
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("The desktop UI needs PySide6: pip install \"screenpilot[ui]\"", file=sys.stderr)
        return 3

    from PySide6.QtCore import QTimer

    from screenpilot.app import create_app
    from screenpilot.platform.autostart import Autostart, set_app_user_model_id
    from screenpilot.ui.controller import AppController
    from screenpilot.ui.qt_helpers import app_icon
    from screenpilot.ui.tray import TrayIcon

    autostarted = "--autostart" in args
    set_app_user_model_id()  # own taskbar button + icon, separate from other pythonw apps
    from screenpilot.platform.win32_window import disable_ghosting

    disable_ghosting()  # a busy overlay must never be swapped for a "Not responding" ghost
    phase("startup.imports")
    qt_app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
    qt_app.setApplicationName("ScreenPilot")
    qt_app.setOrganizationName("ScreenPilot")
    qt_app.setQuitOnLastWindowClosed(False)  # lives in the tray
    qt_app.setWindowIcon(app_icon())

    phase("startup.qt")
    app = create_app()
    phase("startup.core")
    app.diagnostics.install_crash_handlers()
    _install_qt_message_handler(app)

    from screenpilot.ui.theme import ThemeManager
    from screenpilot.ui.widgets import set_theme_manager

    themes = ThemeManager(app.settings.get("general.theme"))
    set_theme_manager(themes)
    themes.apply()
    app.settings.add_listener(lambda key, value: themes.set_mode(value) if key == "general.theme" else None)

    from screenpilot.instance import InstanceServer
    from screenpilot.platform import devpilot

    controller = AppController(app)
    server = InstanceServer(app.paths.data_dir, controller.accept_remote)
    if not server.start():  # another instance won the race while we were starting
        send(app.paths.data_dir, command)
        controller.shutdown(persist=False)
        return 0
    qt_app.aboutToQuit.connect(server.stop)

    if devpilot.owns("autostart"):
        log.info("Started by DevPilot: it owns 'start with Windows'; the Run entry is left alone")
    else:
        autostart = Autostart()
        try:
            autostart.sync(bool(app.settings.get("general.start_with_windows")))
        except OSError:
            log.exception("Could not update the autostart entry")

        def on_autostart(key: str, value) -> None:
            if key == "general.start_with_windows":
                try:
                    autostart.set_enabled(bool(value))
                except OSError:
                    log.exception("Could not update the autostart entry")

        app.settings.add_listener(on_autostart)

    phase("startup.controller")

    # Something visible first: a start from the Start menu, the Desktop or DevPilot opens the
    # Capture Hub right away (the tray icon and the capture bar follow a moment later).
    initial = command if command.get("cmd") == "action" and command.get("action") != "app.show_hub" else None
    if initial is not None:
        QTimer.singleShot(400, lambda: controller.run_remote(initial))
    elif not app.settings.get("general.first_run_completed"):
        controller.show_wizard()
    elif autostarted and app.settings.get("general.start_minimized"):
        log.info("Started with Windows: staying in the tray")
    else:
        controller.show_hub()
        qt_app.processEvents()
    phase("startup.window")

    def finish_startup() -> None:
        tray = TrayIcon(controller)
        controller.tray = tray
        if devpilot.owns("tray"):
            log.info("Started by DevPilot: no tray icon (DevPilot's hub shows ScreenPilot's options)")
        else:
            tray.show()
        phase("startup.tray")
        from screenpilot.ui.edge_bar import EdgeBar

        if app.policy.allows("edge_bar"):
            edge_bar = EdgeBar(controller)
            controller.edge_bar = edge_bar
            edge_bar.start()
        phase("startup.edge_bar")
        app.diagnostics.record("app.startup", (time.perf_counter() - started) * 1000, **phases)
        log.info("Desktop UI ready (%s)", ", ".join(f"{k} {v:.0f} ms" for k, v in phases.items()))

    QTimer.singleShot(0, finish_startup)
    return qt_app.exec()


def _install_qt_message_handler(app) -> None:
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    qt_log = logging.getLogger("screenpilot.qt")
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, context, message) -> None:
        where = f"{context.file}:{context.line}" if context.file else ""
        qt_log.log(levels.get(mode, logging.WARNING), "%s %s", message, where)
        if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            app.diagnostics.write_crash("Qt critical message", f"{message} {where}")

    qInstallMessageHandler(handler)


if __name__ == "__main__":
    sys.exit(main())
