"""Catalogue of buttons the edge capture bar can show (no Qt here: also used by tests).

The user picks and orders them in *Settings › General › Capture bar buttons*
(``general.edge_bar_buttons``); unknown ids are ignored, so a newer or older
settings file never breaks the bar.
"""

from __future__ import annotations

from dataclasses import dataclass

SEPARATOR = "|"


@dataclass(frozen=True)
class EdgeItem:
    icon: str
    label: str | None  # short text under the icon (None = icon only)
    tooltip: str
    action: str | None = None  # action id checked against the IT policy


EDGE_ITEMS: dict[str, EdgeItem] = {
    "capture.region": EdgeItem("region", "hub.mode.region", "action.capture.region", "capture.region"),
    "capture.window": EdgeItem("window", "hub.mode.window", "action.capture.window", "capture.window"),
    "capture.fullscreen": EdgeItem("monitor", "hub.mode.screen", "action.capture.fullscreen", "capture.fullscreen"),
    "capture.all_monitors": EdgeItem("monitors", "hub.mode.all", "action.capture.all_monitors", "capture.all_monitors"),
    "capture.scrolling": EdgeItem("scroll", "hub.mode.scrolling", "action.capture.scrolling", "capture.scrolling"),
    "delayed": EdgeItem("timer", "hub.delay", "edgebar.delay", "capture.delayed"),
    "capture.edit": EdgeItem("edit", "edgebar.item.edit", "action.capture.edit", "capture.edit"),
    "capture.steps": EdgeItem("steps", "edgebar.item.steps", "action.capture.steps", "capture.steps"),
    "capture.copy": EdgeItem("copy", "edgebar.item.copy", "action.capture.copy", "capture.copy"),
    "capture.pin": EdgeItem("pin", "edgebar.item.pin", "action.capture.pin", "capture.pin"),
    "capture.repeat_last": EdgeItem("redo", "edgebar.item.repeat", "action.capture.repeat_last", "capture.repeat_last"),
    "ocr.region": EdgeItem("ocr", "edgebar.item.ocr", "action.ocr.region", "ocr.region"),
    "record.video": EdgeItem("video", "edgebar.video", "edgebar.video.tip", "record.video"),
    "record.gif": EdgeItem("film", "hub.mode.gif", "action.record.gif", "record.gif"),
    "procedure.toggle": EdgeItem("procedure", "hub.mode.procedure", "action.procedure.toggle", "procedure.toggle"),
    "tools.color": EdgeItem("sparkle", "edgebar.item.color", "action.tools.color", "tools.color"),
    "tools.measure": EdgeItem("line", "edgebar.item.measure", "action.tools.measure", "tools.measure"),
    "app.open_editor": EdgeItem("image", "edgebar.item.editor", "action.app.open_editor", "app.open_editor"),
    "app.edit_video": EdgeItem("film", "edgebar.item.edit_video", "action.app.edit_video", "app.edit_video"),
    "hub": EdgeItem("logo", None, "edgebar.hub"),
    "history": EdgeItem("history", None, "edgebar.history"),
    "settings": EdgeItem("settings", None, "action.app.settings"),
    "hide": EdgeItem("eye_off", None, "edgebar.hide"),
}

DEFAULT_EDGE_BUTTONS: list[str] = [
    "capture.region",
    "capture.window",
    "capture.fullscreen",
    "capture.all_monitors",
    "capture.scrolling",
    "delayed",
    "record.video",
    SEPARATOR,
    "hub",
    "history",
    "hide",
]


def edge_buttons(value) -> list[str]:
    """Clean list from the setting: known ids once each, no doubled/edge separators."""
    if not isinstance(value, (list, tuple)) or not value:
        value = DEFAULT_EDGE_BUTTONS
    out: list[str] = []
    for item in value:
        item = str(item)
        if item == SEPARATOR:
            if out and out[-1] != SEPARATOR:
                out.append(item)
        elif item in EDGE_ITEMS and item not in out:
            out.append(item)
    while out and out[-1] == SEPARATOR:
        out.pop()
    return out or list(DEFAULT_EDGE_BUTTONS)
