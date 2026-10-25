"""Backup and restore for a running :class:`ScreenPilotApp` (headless).

Thin layer over :mod:`screenpilot.core.backup`: it takes the live settings
and hotkeys from the app, remembers the last backup time, makes the safety
copy before a restore and reloads what a restore changed, so the UI, the CLI
and automation all behave the same.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from screenpilot.core import backup as core
from screenpilot.core.errors import HotkeyError

if TYPE_CHECKING:  # pragma: no cover
    from screenpilot.app.container import ScreenPilotApp

log = logging.getLogger(__name__)


def folder(app: "ScreenPilotApp") -> Path:
    return core.backup_folder(app.paths, str(app.settings.get("backup.folder") or ""))


def keep(app: "ScreenPilotApp") -> int:
    value = app.settings.get("backup.keep")
    return max(1, min(core.MAX_KEEP, int(value))) if isinstance(value, int) else core.DEFAULT_KEEP


def is_due(app: "ScreenPilotApp", now: datetime | None = None) -> bool:
    return core.is_due(str(app.settings.get("backup.mode")), str(app.settings.get("backup.last") or ""), now)


def make_backup(app: "ScreenPilotApp", *, now: datetime | None = None) -> core.BackupResult:
    """Write a backup now (no settings are changed; call :func:`remember` afterwards)."""
    return core.create_backup(
        app.paths,
        folder(app),
        settings_data=app.settings.as_dict(),
        hotkeys_data=app.hotkeys.export_profile(),
        keep=keep(app),
        now=now,
    )


def remember(app: "ScreenPilotApp", result: core.BackupResult) -> None:
    """Store the time of a successful backup (GUI thread: settings listeners may touch widgets)."""
    try:
        app.settings.set("backup.last", result.created.isoformat(timespec="seconds"))
        app.settings.save()
    except Exception:  # noqa: BLE001 - the backup itself is done
        log.exception("Could not store the last backup time")


def backup_now(app: "ScreenPilotApp", *, now: datetime | None = None) -> core.BackupResult:
    result = make_backup(app, now=now)
    remember(app, result)
    return result


def plan_restore(app: "ScreenPilotApp", path: Path) -> core.RestorePlan:
    contents = core.read_backup(path)
    return core.plan_restore(
        contents,
        app.paths,
        current_settings=app.settings.as_dict(),
        current_hotkeys=app.hotkeys.export_profile(),
        adapt_paths=bool(app.settings.get("backup.adapt_paths")),
    )


@dataclass(frozen=True)
class RestoreResult:
    safety: Path
    settings_changed: list[str]
    hotkeys_changed: bool


def restore(app: "ScreenPilotApp", plan: core.RestorePlan) -> RestoreResult:
    """Safety backup first, then write the files and reload them in the running app."""
    safety = core.create_backup(
        app.paths,
        folder(app),
        settings_data=app.settings.as_dict(),
        hotkeys_data=app.hotkeys.export_profile(),
        safety=True,
    ).path
    core.apply_restore(plan, app.paths)
    changed: list[str] = []
    if plan.settings is not None and plan.settings_changes:
        changed = app.settings.replace(plan.settings)
        try:
            app.settings.save()
        except OSError:
            log.exception("Could not save the restored settings")
    hotkeys_changed = False
    if plan.hotkeys is not None and plan.hotkey_changes:
        try:
            for warning in app.hotkeys.import_profile(plan.hotkeys):
                log.warning("Restored hotkeys: %s", warning)
            hotkeys_changed = True
        except HotkeyError as exc:
            log.warning("Hotkeys in the backup were not restored: %s", exc)
    if plan.history is not None:
        app.history.reload()
    return RestoreResult(safety=safety, settings_changed=changed, hotkeys_changed=hotkeys_changed)
