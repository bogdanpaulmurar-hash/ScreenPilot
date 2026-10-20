"""Administrator policy (IT-enforced settings), read-only for the user.

ScreenPilot looks for ``policy.json`` in, by priority:
1. ``%ProgramData%\\ScreenPilot\\policy.json`` (deployed by IT, e.g. via GPO/Intune)
2. the application folder (portable deployments)

Format::

    {
      "locked": {                       # setting key -> forced value
        "capture.output_dir": "\\\\server\\share\\captures",
        "privacy.allow_network": false
      },
      "disabled_features": ["run_script", "edge_bar", "scrolling", "recording", "record_audio", "ocr", "pin"],
      "message": "Managed by Contoso IT"
    }

Locked settings are shown greyed out with the message; disabled features are
hidden from menus and refused by the controller.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

POLICY_FILE = "policy.json"
FEATURES = (
    "run_script",
    "edge_bar",
    "scrolling",
    "recording",
    "record_audio",
    "record_annotations",
    "ocr",
    "pin",
    "watermark_off",
    "export_diagnostics",
)


@dataclass(frozen=True)
class Policy:
    locked: dict[str, Any] = field(default_factory=dict)
    disabled: frozenset[str] = frozenset()
    message: str = ""
    source: str = ""

    def is_locked(self, key: str) -> bool:
        return key in self.locked

    def allows(self, feature: str) -> bool:
        return feature not in self.disabled

    @property
    def active(self) -> bool:
        return bool(self.locked or self.disabled)


def candidate_paths(app_dir: Path | None = None) -> list[Path]:
    paths = []
    if sys.platform == "win32":
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        paths.append(Path(program_data) / "ScreenPilot" / POLICY_FILE)
    override = os.environ.get("SCREENPILOT_POLICY")
    if override:
        paths.insert(0, Path(override))
    if app_dir is not None:
        paths.append(app_dir / POLICY_FILE)
    return paths


def load_policy(paths: list[Path] | None = None) -> Policy:
    for path in paths if paths is not None else candidate_paths():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("Ignoring unreadable policy file %s: %s", path, exc)
            continue
        locked = data.get("locked") or {}
        disabled = data.get("disabled_features") or []
        if not isinstance(locked, dict) or not isinstance(disabled, list):
            log.error("Ignoring malformed policy file %s", path)
            continue
        unknown = [f for f in disabled if f not in FEATURES]
        if unknown:
            log.warning("Policy %s disables unknown features: %s", path, unknown)
        log.info("Policy loaded from %s (%d locked settings, %d disabled features)", path, len(locked), len(disabled))
        return Policy(dict(locked), frozenset(str(f) for f in disabled), str(data.get("message", "")), str(path))
    return Policy()
