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

from typing import TYPE_CHECKING

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from screenpilot import __version__
from screenpilot.capture import CaptureBackend, CaptureResult, CaptureService, default_backend
from screenpilot.core.events import EventBus
from screenpilot.core import diagnostics as diag_mod
from screenpilot.core.diagnostics import Diagnostics
from screenpilot.core.logging_setup import close_logging, configure_logging, set_level
from screenpilot.core.paths import AppPaths, application_dir, resolve_paths
from screenpilot.core.policy import Policy, candidate_paths, load_policy
from screenpilot.core.services import ServiceRegistry
from screenpilot.history import CaptureHistory
from screenpilot.hotkeys import HotkeyManager
from screenpilot.i18n import Translator
from screenpilot.recording import RecordingService
from screenpilot.platform import Clipboard, DefaultShell, Shell, default_clipboard
from screenpilot.settings import Settings
from screenpilot.workflow import ActionRegistry, StepResult, Workflow, WorkflowContext

log = logging.getLogger(__name__)


if TYPE_CHECKING:  # pragma: no cover
    from screenpilot.videoedit import VideoEditService


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
    diagnostics: Diagnostics = field(default_factory=lambda: Diagnostics(None))
    policy: Policy = field(default_factory=Policy)
    services: ServiceRegistry = field(default_factory=ServiceRegistry)
    recording: RecordingService | None = None
    video: "VideoEditService | None" = None

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
        if self.settings.get("watermark.enabled") and not result.metadata.get("watermarked"):
            from screenpilot.editor.watermark import apply_watermark

            result = apply_watermark(result, self.settings)
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
        chosen = steps if steps is not None else self.settings.get("workflow.after_capture")
        if not self.policy.allows("run_script"):
            chosen = [s for s in chosen if s.get("id") != "run_script"]
        workflow = Workflow(
            chosen,
            stop_on_error=self.settings.get("workflow.stop_on_error"),
        )
        return workflow.run(ctx, self.actions), ctx

    def shutdown(self, *, persist: bool = True) -> None:
        """``persist=False`` for command-line runs: they must not overwrite settings the
        desktop app (possibly running at the same time) changed meanwhile."""
        if persist:
            try:
                self.settings.save()
                self.hotkeys.save(self.paths.hotkeys_file)
            except OSError:
                log.exception("Could not persist state on shutdown")
        log.info("ScreenPilot stopped")
        self.diagnostics.close()
        close_logging()

    def export_diagnostics(self, target: Path) -> Path:
        settings = self.settings.as_dict()
        return diag_mod.export_bundle(
            target,
            log_dir=self.paths.log_dir,
            settings=settings,
            diagnostics=self.diagnostics,
            extra={
                "monitors": [str(m) for m in self._safe_monitors()],
                "hotkeys": {a: str(h) if h else None for a, h in self.hotkeys.bindings().items()},
                "hotkey_status": {a: s.value for a, s in self.hotkeys.diagnostics().items()},
                "backend": getattr(self.capture.backend, "name", "?"),
                "dpi_mode": getattr(self.capture.backend, "dpi_mode", None),
                "ffmpeg": self._ffmpeg_summary(),
            },
        )

    def _ffmpeg_summary(self) -> dict | None:
        try:
            info = self.recording.ffmpeg() if self.recording else None
        except Exception:  # noqa: BLE001 - diagnostics must never fail
            return None
        if info is None:
            return None
        return {"path": str(info.path), "version": info.version, "h264": sorted(e for e in info.encoders if "264" in e)}

    def _safe_monitors(self):
        try:
            return self.capture.monitors()
        except Exception:  # noqa: BLE001
            return []


def create_app(
    *,
    paths: AppPaths | None = None,
    backend: CaptureBackend | None = None,
    clipboard: Clipboard | None = None,
    shell: Shell | None = None,
    language: str | None = None,
    console_log: bool = False,
    policy: Policy | None = None,
) -> ScreenPilotApp:
    paths = (paths or resolve_paths()).ensure()
    configure_logging(paths.log_dir, console=console_log)
    log.info("ScreenPilot %s starting (portable=%s)", __version__, paths.portable)

    diagnostics = Diagnostics(paths.log_dir)
    diag_mod.set_current(diagnostics)
    settings = Settings(paths.settings_file).load()
    policy = policy if policy is not None else load_policy(candidate_paths(application_dir()))
    settings.locked = dict(policy.locked)
    if settings.get("diagnostics.verbose_logging"):
        set_level(logging.DEBUG)
    settings.add_listener(
        lambda key, value: set_level(logging.DEBUG if value else logging.INFO)
        if key == "diagnostics.verbose_logging"
        else None
    )
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
        diagnostics=diagnostics,
        policy=policy,
    )
    app.history.attach(bus)
    app.recording = RecordingService(
        settings,
        backend=app.capture.backend,
        bus=bus,
        policy=policy,
        default_dir=lambda: app.output_dir / "Recordings",
        app_dir=application_dir(),
        extra_dirs=(paths.data_dir,),
    )
    from screenpilot.videoedit import VideoEditService

    app.video = VideoEditService(app.recording, bus=bus, policy=policy, settings=settings)
    backend_obj = app.capture.backend
    if hasattr(backend_obj, "include_cursor"):
        backend_obj.include_cursor = bool(settings.get("capture.include_cursor"))
        settings.add_listener(
            lambda key, value: setattr(backend_obj, "include_cursor", bool(value)) if key == "capture.include_cursor" else None
        )
    settings.add_listener(lambda key, value: app.history.set_limit(value) if key == "history.limit" else None)
    for key, instance in (
        (Settings, settings),
        (Translator, translator),
        (EventBus, bus),
        (CaptureService, app.capture),
        (HotkeyManager, hotkeys),
        (ActionRegistry, app.actions),
        (CaptureHistory, app.history),
        (RecordingService, app.recording),
        (VideoEditService, app.video),
    ):
        app.services.register_instance(key, instance)
    return app
