"""Application composition root.

:func:`create_app` wires the engines together and returns a
:class:`ScreenPilotApp` facade. It is the single entry point for the desktop UI,
the CLI and external automation (e.g. Autopilot)::

    from screenpilot.app import create_app
    app = create_app()
    result = app.capture.capture_region(Rect(0, 0, 800, 600))
    app.run_after_capture(result)            # user's configured action chain
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from screenpilot import __version__
from screenpilot.capture import CaptureBackend, CaptureResult, CaptureService, default_backend
from screenpilot.core.events import EventBus
from screenpilot.core.logging_setup import close_logging, configure_logging
from screenpilot.core.paths import AppPaths, resolve_paths
from screenpilot.core.services import ServiceRegistry
from screenpilot.history import CaptureHistory
from screenpilot.hotkeys import HotkeyManager
from screenpilot.i18n import Translator
from screenpilot.platform import Clipboard, DefaultShell, Shell, default_clipboard
from screenpilot.settings import Settings
from screenpilot.workflow import ActionRegistry, StepResult, Workflow, WorkflowContext

log = logging.getLogger(__name__)


@dataclass
class ScreenPilotApp:
    paths: AppPaths
    settings: Settings
    translator: Translator
    bus: EventBus
    capture: CaptureService
    hotkeys: HotkeyManager
    actions: ActionRegistry
    clipboard: Clipboard
    shell: Shell
    history: CaptureHistory
    services: ServiceRegistry = field(default_factory=ServiceRegistry)

    @property
    def output_dir(self) -> Path:
        configured = self.settings.get("capture.output_dir")
        return Path(configured) if configured else self.paths.captures_dir

    def run_after_capture(
        self,
        result: CaptureResult,
        steps: list[dict] | None = None,
        *,
        ask_save_path: Callable[[Path], Path | None] | None = None,
    ) -> tuple[list[StepResult], WorkflowContext]:
        ctx = WorkflowContext(
            result=result,
            output_dir=self.output_dir,
            clipboard=self.clipboard,
            shell=self.shell,
            bus=self.bus,
            filename_template=self.settings.get("capture.filename_template"),
            image_format=self.settings.get("capture.image_format"),
            jpeg_quality=self.settings.get("capture.jpeg_quality"),
            ask_save_path=ask_save_path,
        )
        workflow = Workflow(
            steps if steps is not None else self.settings.get("workflow.after_capture"),
            stop_on_error=self.settings.get("workflow.stop_on_error"),
        )
        return workflow.run(ctx, self.actions), ctx

    def shutdown(self) -> None:
        try:
            self.settings.save()
            self.hotkeys.save(self.paths.hotkeys_file)
        except OSError:
            log.exception("Could not persist state on shutdown")
        log.info("ScreenPilot stopped")
        close_logging()


def create_app(
    *,
    paths: AppPaths | None = None,
    backend: CaptureBackend | None = None,
    clipboard: Clipboard | None = None,
    shell: Shell | None = None,
    language: str | None = None,
    console_log: bool = False,
) -> ScreenPilotApp:
    paths = (paths or resolve_paths()).ensure()
    configure_logging(paths.log_dir, console=console_log)
    log.info("ScreenPilot %s starting (portable=%s)", __version__, paths.portable)

    settings = Settings(paths.settings_file).load()
    translator = Translator(language or settings.get("general.language"))
    # Keep the UI language in sync with the setting, live.
    settings.add_listener(lambda key, value: translator.set_language(value) if key == "general.language" else None)

    bus = EventBus()
    hotkeys = HotkeyManager()
    for warning in hotkeys.load(paths.hotkeys_file):
        log.warning("Hotkey profile: %s", warning)

    app = ScreenPilotApp(
        paths=paths,
        settings=settings,
        translator=translator,
        bus=bus,
        capture=CaptureService(backend or default_backend(), bus),
        hotkeys=hotkeys,
        actions=ActionRegistry(),
        clipboard=clipboard or default_clipboard(),
        shell=shell or DefaultShell(),
        history=CaptureHistory(paths.data_dir, limit=settings.get("history.limit")).load(),
    )
    app.history.attach(bus)
    settings.add_listener(lambda key, value: app.history.set_limit(value) if key == "history.limit" else None)
    for key, instance in (
        (Settings, settings),
        (Translator, translator),
        (EventBus, bus),
        (CaptureService, app.capture),
        (HotkeyManager, hotkeys),
        (ActionRegistry, app.actions),
        (CaptureHistory, app.history),
    ):
        app.services.register_instance(key, instance)
    return app
