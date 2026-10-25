"""Backup and restore of the user's settings and saved context (headless).

A backup is a single zip in a folder the user chooses::

    ScreenPilot-backup-20261025-071500.zip
        manifest.json              format, version, where the files came from
        config/settings.json       all settings
        config/hotkeys.json        hotkey profile
        data/history.json          capture history index (not the captures)
        data/thumbnails/<id>.png   history thumbnails
        data/procedures/<id>/...   procedures still being recorded

Never included: the captures and recordings themselves (they already live as
files in the user's folder), logs, temporary drag files, the single-instance
key and the administrator policy.

Nothing here imports Qt, so automation clients can call it too.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any

from screenpilot import __version__
from screenpilot.core.paths import AppPaths

log = logging.getLogger(__name__)

FORMAT = "screenpilot.backup"
VERSION = 1
PREFIX = "ScreenPilot-backup-"
SAFETY_SUFFIX = "-before-restore"
NAME_RE = re.compile(r"^ScreenPilot-backup-\d{8}-\d{6}(?:-\d+)?\.zip$", re.IGNORECASE)

MODES = ("daily", "every_start", "manual")
DEFAULT_KEEP = 10
MAX_KEEP = 999
MAX_MEMBER_BYTES = 200 * 1024 * 1024  # refuse absurd members (zip bombs)

CLOUD_MARKERS = ("onedrive", "google drive", "googledrive", "my drive", "dropbox", "icloud drive", "box sync")


class BackupError(Exception):
    """A backup could not be made or read; ``message_key`` is an i18n key."""

    def __init__(self, message: str, message_key: str = "backup.error.generic") -> None:
        super().__init__(message)
        self.message_key = message_key


# -- locations ------------------------------------------------------------------
def default_folder(paths: AppPaths) -> Path:
    if paths.portable:
        return paths.config_dir.parent / "Backups"
    return Path.home() / "Documents" / "ScreenPilot Backups"


def backup_folder(paths: AppPaths, configured: str) -> Path:
    return Path(os.path.expandvars(configured)).expanduser() if configured.strip() else default_folder(paths)


def is_cloud_folder(path: Path) -> bool:
    """True for folders a sync client (OneDrive, Google Drive...) may lock while writing."""
    text = str(path).replace("\\", "/").lower()
    return any(marker in part for part in text.split("/") for marker in CLOUD_MARKERS)


# -- schedule -------------------------------------------------------------------
def is_due(mode: str, last: str, now: datetime | None = None) -> bool:
    """Whether the automatic start-up backup should run."""
    if mode == "every_start":
        return True
    if mode != "daily":
        return False
    if not last:
        return True
    try:
        return datetime.fromisoformat(last).date() != (now or datetime.now()).date()
    except ValueError:
        return True


# -- creating -------------------------------------------------------------------
@dataclass(frozen=True)
class BackupResult:
    path: Path
    created: datetime
    files: int
    deleted: tuple[Path, ...] = ()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _unique_target(folder: Path, stamp: str, suffix: str) -> Path:
    target = folder / f"{PREFIX}{stamp}{suffix}.zip"
    n = 2
    while target.exists():  # two backups in the same second: ...-2.zip, ...-3.zip
        target = folder / f"{PREFIX}{stamp}{suffix}-{n}.zip"
        n += 1
    return target


def create_backup(
    paths: AppPaths,
    folder: Path,
    *,
    settings_data: dict[str, Any] | None = None,
    hotkeys_data: dict[str, Any] | None = None,
    keep: int | None = None,
    safety: bool = False,
    now: datetime | None = None,
) -> BackupResult:
    """Write a backup zip into ``folder``.

    ``settings_data`` / ``hotkeys_data`` are the live values from the running
    app; when omitted the files on disk are used. ``keep`` rotates old backups
    (safety copies made before a restore are never rotated). The zip is written
    to a temporary file first, so a failure never leaves a half backup.
    """
    now = now or datetime.now()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"Cannot create {folder}: {exc}", "backup.error.folder") from exc

    settings = settings_data if settings_data is not None else _read_json(paths.settings_file)
    hotkeys = hotkeys_data if hotkeys_data is not None else _read_json(paths.hotkeys_file)
    history = _read_json(paths.data_dir / "history.json")

    members: list[tuple[str, Path]] = []
    thumbs = paths.data_dir / "thumbnails"
    if thumbs.is_dir():
        members += [(f"data/thumbnails/{p.name}", p) for p in sorted(thumbs.glob("*.png")) if p.is_file()]
    procedures = paths.data_dir / "procedures"
    if procedures.is_dir():
        for p in sorted(procedures.rglob("*")):
            if p.is_file() and not p.name.endswith(".tmp"):
                members.append(("data/procedures/" + p.relative_to(procedures).as_posix(), p))

    history_count = len(history.get("entries", [])) if isinstance(history, dict) else 0
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "app_version": __version__,
        "created": now.isoformat(timespec="seconds"),
        "home": str(Path.home()),
        "config_dir": str(paths.config_dir),
        "data_dir": str(paths.data_dir),
        "captures_dir": str(paths.captures_dir),
        "portable": paths.portable,
        "contents": {
            "settings": settings is not None,
            "hotkeys": hotkeys is not None,
            "history_entries": history_count,
            "thumbnails": sum(1 for name, _ in members if name.startswith("data/thumbnails/")),
            "procedures": len(_procedure_ids(name for name, _ in members)),
        },
    }

    target = _unique_target(folder, now.strftime("%Y%m%d-%H%M%S"), SAFETY_SUFFIX if safety else "")
    fd, tmp = tempfile.mkstemp(prefix=".screenpilot-backup-", suffix=".tmp", dir=folder)
    os.close(fd)
    files = 0
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            for name, data in (
                ("config/settings.json", settings),
                ("config/hotkeys.json", hotkeys),
                ("data/history.json", history),
            ):
                if data is not None:
                    zf.writestr(name, json.dumps(data, indent=2, ensure_ascii=False))
                    files += 1
            for name, source in members:
                try:
                    zf.write(source, name)
                    files += 1
                except OSError as exc:  # a file vanished or is locked: skip it, keep the rest
                    log.warning("Backup skipped %s: %s", source, exc)
        os.replace(tmp, target)
    except OSError as exc:
        Path(tmp).unlink(missing_ok=True)
        raise BackupError(f"Cannot write the backup in {folder}: {exc}", "backup.error.write") from exc
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    deleted = tuple(rotate(folder, keep)) if keep is not None and not safety else ()
    log.info("Backup written: %s (%d files)", target, files)
    return BackupResult(path=target, created=now, files=files, deleted=deleted)


def _procedure_ids(names) -> set[str]:
    ids = set()
    for name in names:
        parts = name.split("/")
        if len(parts) >= 4 and parts[0] == "data" and parts[1] == "procedures":
            ids.add(parts[2])
    return ids


def list_backups(folder: Path) -> list[Path]:
    """Regular backups in ``folder``, newest first (safety copies excluded)."""
    if not folder.is_dir():
        return []
    found = [p for p in folder.iterdir() if p.is_file() and NAME_RE.match(p.name)]

    def order(p: Path) -> tuple[str, int]:
        stem = p.stem[len(PREFIX) :]  # 20261025-071500 or 20261025-071500-2
        stamp, _, n = stem[:15], stem[15:16], stem[16:]
        return stamp, int(n) if n.isdigit() else 1

    return sorted(found, key=order, reverse=True)


def rotate(folder: Path, keep: int) -> list[Path]:
    """Delete the oldest regular backups beyond ``keep``; other files are left alone."""
    keep = max(1, min(MAX_KEEP, int(keep)))
    deleted = []
    for old in list_backups(folder)[keep:]:
        try:
            old.unlink()
            deleted.append(old)
        except OSError as exc:
            log.warning("Could not delete old backup %s: %s", old, exc)
    return deleted


# -- reading --------------------------------------------------------------------
@dataclass
class BackupContents:
    """What a backup zip holds, read and validated (nothing applied yet)."""

    path: Path
    manifest: dict[str, Any]
    settings: dict[str, Any] | None = None
    hotkeys: dict[str, Any] | None = None
    history: dict[str, Any] | None = None
    thumbnails: dict[str, bytes] = field(default_factory=dict)  # file name -> data
    procedures: dict[str, dict[str, bytes]] = field(default_factory=dict)  # id -> relative path -> data

    @property
    def created(self) -> str:
        return str(self.manifest.get("created", ""))


def _safe_name(name: str) -> bool:
    pure = PurePath(name)
    return not (name.startswith(("/", "\\")) or ":" in name or ".." in pure.parts or "\\" in name)


def read_backup(path: Path) -> BackupContents:
    """Open and validate a backup zip; raises :class:`BackupError` if it is not one."""
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError(f"{path} is not a ScreenPilot backup: {exc}", "backup.error.not_backup") from exc
    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise BackupError(f"{path} has no valid manifest", "backup.error.not_backup") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise BackupError(f"{path} is not a ScreenPilot backup", "backup.error.not_backup")
        version = manifest.get("version")
        if not isinstance(version, int) or version > VERSION:
            raise BackupError(f"{path} was made by a newer ScreenPilot", "backup.error.newer")

        contents = BackupContents(path=path, manifest=manifest)
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or name == "manifest.json":
                continue
            if not _safe_name(name) or info.file_size > MAX_MEMBER_BYTES:
                log.warning("Backup member ignored: %s", name)
                continue
            data = zf.read(info)
            if name in ("config/settings.json", "config/hotkeys.json", "data/history.json"):
                try:
                    value = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    log.warning("Backup member unreadable: %s", name)
                    continue
                if not isinstance(value, dict):
                    continue
                if name == "config/settings.json":
                    contents.settings = value
                elif name == "config/hotkeys.json":
                    contents.hotkeys = value
                else:
                    contents.history = value
            elif name.startswith("data/thumbnails/") and name.count("/") == 2:
                contents.thumbnails[name.rsplit("/", 1)[1]] = data
            elif name.startswith("data/procedures/"):
                parts = name.split("/", 3)
                if len(parts) == 4 and parts[3]:
                    contents.procedures.setdefault(parts[2], {})[parts[3]] = data
        return contents


# -- restoring ------------------------------------------------------------------
# Settings that describe this PC, not the user's preferences: a restore keeps the local value.
LOCAL_KEYS = ("general.start_with_windows", "general.first_run_completed", "backup")
PATH_KEYS = ("capture.output_dir", "recording.output_dir", "recording.ffmpeg_path")


def _norm(text: str) -> str:
    return text.replace("\\", "/").rstrip("/")


def remap_path(value: str, pairs: list[tuple[str, str]]) -> str:
    """Rewrite ``value`` when it starts with one of the old prefixes (case-insensitive)."""
    if not value:
        return value
    v = _norm(value)
    for old, new in pairs:
        o = _norm(old)
        if o and (v.lower() == o.lower() or v.lower().startswith(o.lower() + "/")):
            sep = "\\" if ("\\" in new or ":" in new) else "/"
            rest = v[len(o) :].replace("/", sep)
            return new.rstrip("\\/") + rest
    return value


def _get(tree: dict[str, Any], dotted: str) -> Any:
    node: Any = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(tree: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = tree
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


@dataclass
class RestorePlan:
    """Everything a restore would change, computed without touching any file."""

    contents: BackupContents
    settings: dict[str, Any] | None = None
    settings_changes: list[str] = field(default_factory=list)
    hotkeys: dict[str, Any] | None = None
    hotkey_changes: list[str] = field(default_factory=list)
    history: dict[str, Any] | None = None
    history_added: list[dict[str, Any]] = field(default_factory=list)
    history_missing: list[str] = field(default_factory=list)  # capture files not found on this PC
    procedures_new: list[str] = field(default_factory=list)
    procedures_skipped: list[str] = field(default_factory=list)  # already here, left untouched
    remapped: list[tuple[str, str]] = field(default_factory=list)  # (old, new) folders used for paths

    @property
    def has_changes(self) -> bool:
        return bool(self.settings_changes or self.hotkey_changes or self.history_added or self.procedures_new)


def _path_pairs(manifest: dict[str, Any], paths: AppPaths, home: Path) -> list[tuple[str, str]]:
    pairs = []
    for key, new in (
        ("data_dir", paths.data_dir),
        ("config_dir", paths.config_dir),
        ("captures_dir", paths.captures_dir),
        ("home", home),
    ):
        old = manifest.get(key)
        if isinstance(old, str) and old and _norm(old).lower() != _norm(str(new)).lower():
            pairs.append((old, str(new)))
    return sorted(pairs, key=lambda p: len(_norm(p[0])), reverse=True)


def plan_restore(
    contents: BackupContents,
    paths: AppPaths,
    *,
    current_settings: dict[str, Any],
    current_hotkeys: dict[str, Any] | None = None,
    adapt_paths: bool = True,
    home: Path | None = None,
    exists=Path.exists,
) -> RestorePlan:
    """Compare a backup with the current state and prepare the files to write."""
    from screenpilot.settings.store import Settings, changed_keys

    plan = RestorePlan(contents=contents)
    pairs = _path_pairs(contents.manifest, paths, home or Path.home()) if adapt_paths else []
    plan.remapped = pairs
    normalizer = Settings(None)
    current = normalizer.normalize(current_settings)

    if contents.settings is not None:
        incoming = normalizer.normalize(contents.settings)
        for key in LOCAL_KEYS:
            _set(incoming, key, _get(current, key))
        for key in PATH_KEYS:
            value = _get(incoming, key)
            if isinstance(value, str) and value:
                _set(incoming, key, remap_path(value, pairs))
        ffmpeg = _get(incoming, "recording.ffmpeg_path")
        if ffmpeg and not exists(Path(ffmpeg)):
            _set(incoming, "recording.ffmpeg_path", _get(current, "recording.ffmpeg_path"))
        plan.settings = incoming
        plan.settings_changes = changed_keys(current, incoming)

    if contents.hotkeys is not None and isinstance(contents.hotkeys.get("bindings"), dict):
        plan.hotkeys = contents.hotkeys
        old = (current_hotkeys or {}).get("bindings") or {}
        new = contents.hotkeys["bindings"]
        plan.hotkey_changes = sorted(a for a in set(old) | set(new) if a in new and old.get(a) != new.get(a))

    current_history = _read_json(paths.data_dir / "history.json")
    current_entries = current_history.get("entries", []) if isinstance(current_history, dict) else []
    known = {e.get("id") for e in current_entries if isinstance(e, dict)}
    backup_entries = (contents.history or {}).get("entries", []) if contents.history else []
    for entry in backup_entries:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("path"):
            continue
        entry = dict(entry, path=remap_path(str(entry["path"]), pairs))
        thumb = Path(str(entry.get("thumbnail", "")).replace("\\", "/")).name
        entry["thumbnail"] = str(paths.data_dir / "thumbnails" / thumb) if thumb in contents.thumbnails else ""
        if not exists(Path(entry["path"])):
            plan.history_missing.append(entry["path"])
        elif entry["id"] not in known:
            plan.history_added.append(entry)
    if plan.history_added:
        limit = _get(plan.settings or current, "history.limit") or 100
        merged = sorted(current_entries + plan.history_added, key=lambda e: str(e.get("timestamp", "")), reverse=True)
        plan.history = {"version": 1, "entries": merged[: int(limit)]}

    procedures = paths.data_dir / "procedures"
    for pid in sorted(contents.procedures):
        (plan.procedures_skipped if (procedures / pid).exists() else plan.procedures_new).append(pid)
    return plan


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def apply_restore(plan: RestorePlan, paths: AppPaths) -> None:
    """Write the planned files. The running app reloads them afterwards."""
    try:
        if plan.settings is not None and plan.settings_changes:
            _write_json(paths.settings_file, plan.settings)
        if plan.hotkeys is not None and plan.hotkey_changes:
            _write_json(paths.hotkeys_file, plan.hotkeys)
        if plan.history is not None:
            thumbs = paths.data_dir / "thumbnails"
            thumbs.mkdir(parents=True, exist_ok=True)
            for entry in plan.history_added:
                if entry.get("thumbnail"):
                    name = Path(entry["thumbnail"]).name
                    (thumbs / name).write_bytes(plan.contents.thumbnails[name])
            _write_json(paths.data_dir / "history.json", plan.history)
        root = paths.data_dir / "procedures"
        for pid in plan.procedures_new:
            for rel, data in plan.contents.procedures[pid].items():
                target = root / pid / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    except OSError as exc:
        raise BackupError(f"Restore failed: {exc}", "backup.error.restore") from exc
    log.info(
        "Restored %s: %d settings, %d hotkeys, %d history entries, %d procedures",
        plan.contents.path.name,
        len(plan.settings_changes),
        len(plan.hotkey_changes),
        len(plan.history_added),
        len(plan.procedures_new),
    )
