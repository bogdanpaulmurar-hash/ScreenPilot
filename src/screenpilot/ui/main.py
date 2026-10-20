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


def main(argv: Sequence[str] | None = None) -> int:
    import time

    args = list(argv) if argv is not None else sys.argv
    if "--self-test" in args:
        return self_test()
    started = time.perf_counter()
    try:
        from PySide6.QtCore import QLockFile
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("The desktop UI needs PySide6: pip install \"screenpilot[ui]\"", file=sys.stderr)
        return 3

    from screenpilot.app import create_app
    from screenpilot.platform.autostart import Autostart, set_app_user_model_id
    from screenpilot.ui.controller import AppController
    from screenpilot.ui.qt_helpers import app_icon
    from screenpilot.ui.tray import TrayIcon

    autostarted = "--autostart" in args
    set_app_user_model_id()  # own taskbar button + icon, separate from other pythonw apps
    from screenpilot.platform.win32_window import disable_ghosting

    disable_ghosting()  # a busy overlay must never be swapped for a "Not responding" ghost
    qt_app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
    qt_app.setApplicationName("ScreenPilot")
    qt_app.setOrganizationName("ScreenPilot")
    qt_app.setQuitOnLastWindowClosed(False)  # lives in the tray
    qt_app.setWindowIcon(app_icon())

    app = create_app()
    app.diagnostics.install_crash_handlers()
    _install_qt_message_handler(app)

    from screenpilot.ui.theme import ThemeManager
    from screenpilot.ui.widgets import set_theme_manager

    themes = ThemeManager(app.settings.get("general.theme"))
    set_theme_manager(themes)
    themes.apply()
    app.settings.add_listener(lambda key, value: themes.set_mode(value) if key == "general.theme" else None)

    lock = QLockFile(str(app.paths.data_dir / "screenpilot.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "ScreenPilot", app.translator.t("app.already_running"))
        app.shutdown()
        return 1

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

    controller = AppController(app)
    tray = TrayIcon(controller)
    tray.show()

    from screenpilot.ui.edge_bar import EdgeBar

    if app.policy.allows("edge_bar"):
        edge_bar = EdgeBar(controller)
        controller.edge_bar = edge_bar
        edge_bar.start()
    qt_app.aboutToQuit.connect(lambda: lock.unlock())

    if autostarted:
        log.info("Started with Windows: staying in the tray")
    elif not app.settings.get("general.first_run_completed"):
        controller.show_wizard()
    elif not app.settings.get("general.start_minimized"):
        controller.show_hub()

    app.diagnostics.record("app.startup", (time.perf_counter() - started) * 1000)
    log.info("Desktop UI ready")
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
