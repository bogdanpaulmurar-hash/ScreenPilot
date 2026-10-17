"""Capture modes ("profiles"): one hotkey, one selector colour, one Enter action.

Each mode opens the same region selector, but with its own border colour and
its own default action, so the colour tells you what Enter will do:

* region   → after-capture actions (save/copy…)      accent colour
* edit     → open the editor                         magenta
* steps    → number the steps                        green
* scroll   → scrolling capture                       orange
* copy     → copy only, nothing saved                 purple
* pin      → pin to screen                           teal
* ocr      → extract text                            gold
* record   → record a video / GIF of the area        red / dark orange

Colours, border thickness and dimming are user settings (``selector.*``).
"""

from __future__ import annotations

from dataclasses import dataclass

AUTO_COLOR = ""  # empty = use the Windows / theme accent


@dataclass(frozen=True)
class CaptureMode:
    id: str  # hotkey action id
    action: str  # selector action triggered by Enter / click
    default_color: str
    label_key: str


MODES: tuple[CaptureMode, ...] = (
    CaptureMode("capture.region", "default", AUTO_COLOR, "mode.region"),
    CaptureMode("capture.edit", "edit", "#e3008c", "mode.edit"),
    CaptureMode("capture.steps", "steps", "#13a10e", "mode.steps"),
    CaptureMode("capture.scrolling", "scroll", "#f7630c", "mode.scroll"),
    CaptureMode("capture.copy", "copy", "#8764b8", "mode.copy"),
    CaptureMode("capture.pin", "pin", "#00b7c3", "mode.pin"),
    CaptureMode("ocr.region", "ocr", "#c19c00", "mode.ocr"),
    CaptureMode("tools.color", "color", "#ffffff", "mode.color"),
    CaptureMode("tools.measure", "measure", "#00cc6a", "mode.measure"),
    CaptureMode("capture.interval", "interval", "#e81123", "mode.interval"),
    CaptureMode("record.video", "record", "#d13438", "mode.record"),
    CaptureMode("record.gif", "record_gif", "#ca5010", "mode.record_gif"),
)
BY_ID = {m.id: m for m in MODES}
BY_ACTION = {m.action: m for m in MODES}

THICKNESS_RANGE = (1, 6)
DIM_RANGE = (0, 80)  # percent


def mode_color(settings, mode_id: str) -> str:
    """Configured colour for a mode ('' = accent)."""
    mode = BY_ID.get(mode_id)
    if mode is None:
        return AUTO_COLOR
    colors = settings.get("selector.colors") or {}
    value = colors.get(mode_id, mode.default_color)
    return value if isinstance(value, str) else mode.default_color


def set_mode_color(settings, mode_id: str, color: str) -> None:
    colors = dict(settings.get("selector.colors") or {})
    colors[mode_id] = color
    settings.set("selector.colors", colors)
