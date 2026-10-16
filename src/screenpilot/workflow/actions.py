"""Built-in post-capture actions.

An action is a small object with a stable ``id`` and a ``run(ctx, params)``
method. Actions are configured as serializable specs (``{"id": "save"}``) in
settings, so users, presets and automation clients can chain them without code.
New actions (upload, export PDF/Word/Markdown/HTML, OCR) register the same way.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from screenpilot.capture.image import CaptureResult
from screenpilot.core.errors import WorkflowError
from screenpilot.core.events import EventBus
from screenpilot.platform import Clipboard, Shell

log = logging.getLogger(__name__)

EVENT_OPEN_EDITOR = "editor.open_requested"
SCRIPT_TIMEOUT_S = 60
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


@dataclass
class WorkflowContext:
    result: CaptureResult
    output_dir: Path
    clipboard: Clipboard
    shell: Shell
    bus: EventBus
    filename_template: str = "ScreenPilot_{date}_{time}_{mode}"
    image_format: str = "png"
    jpeg_quality: int = 90
    ask_save_path: Callable[[Path], Path | None] | None = None
    saved_path: Path | None = None
    data: dict[str, Any] = field(default_factory=dict)


class ActionSkipped(Exception):
    """Raised by an action that has nothing to do (not a failure)."""


class Action(Protocol):
    id: str

    def run(self, ctx: WorkflowContext, params: dict[str, Any]) -> None: ...


def sanitize_filename(name: str, max_length: int = 150) -> str:
    name = _INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = f"_{name}"
    return name[:max_length] or "capture"


def render_filename(template: str, result: CaptureResult, now: datetime | None = None) -> str:
    now = now or datetime.now()
    values = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H-%M-%S"),
        "datetime": now.strftime("%Y-%m-%d_%H-%M-%S"),
        "mode": result.mode,
        "width": result.image.width,
        "height": result.image.height,
        "window": result.metadata.get("window_title", ""),
    }

    def substitute(match: re.Match[str]) -> str:
        return str(values.get(match.group(1), match.group(0)))

    return sanitize_filename(re.sub(r"\{(\w+)\}", substitute, template))


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise WorkflowError(f"Could not find a free file name for {path}")


def _extension(image_format: str) -> str:
    return {"jpeg": "jpg"}.get(image_format.lower(), image_format.lower())


class SaveAction:
    id = "save"

    def run(self, ctx: WorkflowContext, params: dict[str, Any]) -> None:
        if ctx.saved_path is not None and not params.get("force"):
            raise ActionSkipped("already saved")
        name = render_filename(params.get("template", ctx.filename_template), ctx.result)
        target = unique_path(ctx.output_dir / f"{name}.{_extension(ctx.image_format)}")
        ctx.saved_path = ctx.result.image.save(target, image_format=ctx.image_format, jpeg_quality=ctx.jpeg_quality)
        log.info("Saved capture to %s", ctx.saved_path)


class SaveAsAction:
    id = "save_as"

    def run(self, ctx: WorkflowContext, params: dict[str, Any]) -> None:
        if ctx.ask_save_path is None:
            raise ActionSkipped("no interactive session")
        suggested = ctx.output_dir / f"{render_filename(ctx.filename_template, ctx.result)}.{_extension(ctx.image_format)}"
        chosen = ctx.ask_save_path(suggested)
        if chosen is None:
            raise ActionSkipped("cancelled by user")
        fmt = chosen.suffix.lstrip(".") or ctx.image_format
        ctx.saved_path = ctx.result.image.save(chosen, image_format=fmt, jpeg_quality=ctx.jpeg_quality)


class CopyImageAction:
    id = "copy_image"

    def run(self, ctx: WorkflowContext, params: dict[str, Any]) -> None:
        ctx.clipboard.set_image(ctx.result.image)


class CopyPathAction:
    id = "copy_path"

    def run(self, ctx: WorkflowContext, params: dict[str, Any]) -> None:
        if ctx.saved_path is None:
            raise WorkflowError("Nothing has been saved yet; add 'save' before 'copy_path'")
        ctx.clipboard.set_text(str(ctx.saved_path))


class OpenEditorAction:
    id = "open_editor"

    def run(self, ctx: WorkflowContext, params: dict[str, Any]) -> None:
        ctx.bus.publish(EVENT_OPEN_EDITOR, result=ctx.result, path=ctx.saved_path)


class OpenFolderAction:
    id = "open_folder"

    def run(self, ctx: WorkflowContext, params: dict[str, Any]) -> None:
        ctx.shell.reveal(ctx.saved_path or ctx.output_dir)


class RunScriptAction:
    """Run a user-configured program with the capture path.

    ``params["command"]`` is an argument list, e.g.
    ``["C:/Tools/upload.exe", "--file", "{path}"]``. It is executed without a
    shell, so file names can never be interpreted as commands.
    """

    id = "run_script"

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self._runner = runner

    def run(self, ctx: WorkflowContext, params: dict[str, Any]) -> None:
        command = params.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
            raise WorkflowError("run_script needs 'command' as a non-empty list of strings")
        if ctx.saved_path is None and any("{path}" in a for a in command):
            raise WorkflowError("run_script uses {path} but nothing has been saved yet")
        args = [a.replace("{path}", str(ctx.saved_path or "")).replace("{mode}", ctx.result.mode) for a in command]
        timeout = params.get("timeout", SCRIPT_TIMEOUT_S)
        log.info("Running post-capture script: %s", args[0])
        try:
            completed = self._runner(args, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkflowError(f"Script failed to run: {exc}") from exc
        if completed.returncode != 0:
            raise WorkflowError(f"Script exited with code {completed.returncode}: {(completed.stderr or '').strip()[:200]}")


def builtin_actions() -> list[Action]:
    return [
        SaveAction(),
        SaveAsAction(),
        CopyImageAction(),
        CopyPathAction(),
        OpenEditorAction(),
        OpenFolderAction(),
        RunScriptAction(),
    ]
