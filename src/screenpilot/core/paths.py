"""Filesystem locations, with portable-mode support.

Portable mode keeps settings, logs and captures next to the application so it
can run from a USB stick or a locked-down corporate profile. It is enabled when
a ``portable.flag`` file sits in the application directory or when the
``SCREENPILOT_PORTABLE`` environment variable is set to ``1``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

PORTABLE_FLAG = "portable.flag"
ENV_PORTABLE = "SCREENPILOT_PORTABLE"
ENV_HOME = "SCREENPILOT_HOME"


def application_dir() -> Path:
    """Directory containing the executable (frozen) or the source checkout."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    log_dir: Path
    captures_dir: Path
    portable: bool

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def hotkeys_file(self) -> Path:
        return self.config_dir / "hotkeys.json"

    def ensure(self) -> "AppPaths":
        for path in (self.config_dir, self.data_dir, self.log_dir, self.captures_dir):
            path.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def under(cls, root: Path, *, portable: bool) -> "AppPaths":
        return cls(
            config_dir=root / "config",
            data_dir=root / "data",
            log_dir=root / "logs",
            captures_dir=root / "captures",
            portable=portable,
        )


def resolve_paths(app_dir: Path | None = None) -> AppPaths:
    app_dir = app_dir or application_dir()

    override = os.environ.get(ENV_HOME)
    if override:
        return AppPaths.under(Path(override), portable=True)

    if os.environ.get(ENV_PORTABLE) == "1" or (app_dir / PORTABLE_FLAG).exists():
        return AppPaths.under(app_dir / "UserData", portable=True)

    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        pictures = Path.home() / "Pictures"
        return AppPaths(
            config_dir=roaming / "ScreenPilot",
            data_dir=local / "ScreenPilot",
            log_dir=local / "ScreenPilot" / "logs",
            captures_dir=pictures / "ScreenPilot",
            portable=False,
        )

    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "screenpilot"
    return AppPaths.under(base, portable=False)
