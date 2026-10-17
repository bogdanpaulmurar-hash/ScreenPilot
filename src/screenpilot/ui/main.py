"""Desktop entry point: ``screenpilot-gui`` or ``python -m screenpilot gui``."""

from __future__ import annotations

import logging
import sys
from typing import Sequence

log = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    import time

    started = time.perf_counter()
    try:
        from PySide6.QtCore import QLockFile
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("The desktop UI needs PySide6: pip install \"screenpilot[ui]\"", file=sys.stderr)
        return 3

    from screenpilot.app import create_app
    from screenpilot.ui.controller import AppController
    from screenpilot.ui.qt_helpers import app_icon
    from screenpilot.ui.tray import TrayIcon

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

    controller = AppController(app)
    tray = TrayIcon(controller)
    tray.show()

    from screenpilot.ui.edge_bar import EdgeBar

    if app.policy.allows("edge_bar"):
        edge_bar = EdgeBar(controller)
        controller.edge_bar = edge_bar
        edge_bar.start()
    qt_app.aboutToQuit.connect(lambda: lock.unlock())

    if not app.settings.get("general.first_run_completed"):
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
