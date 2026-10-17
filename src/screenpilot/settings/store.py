"""Settings engine.

* Defaults live in code (:data:`DEFAULTS`); the JSON file stores the user's
  values. Unknown keys from newer versions are preserved, not discarded.
* Values are type-checked against the default; an invalid value in the file
  is logged and replaced by the default instead of crashing at startup.
* Saves are atomic (temp file + ``os.replace``) so a crash never leaves a
  half-written settings file.
* ``schema_version`` allows forward migrations.
* Listeners are notified with the dotted key that changed (UI live updates).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from screenpilot.core.errors import ConfigurationError

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "general": {
        "language": "en",
        "start_minimized": False,
        "start_with_windows": False,
        "first_run_completed": False,
        "tester_mode": False,
        "theme": "system",
        "edge_bar": True,
        "edge_bar_position": 0.5,
    },
    "capture": {
        "default_delay_seconds": 3,
        "include_cursor": False,
        "show_preview": True,
        "play_sound": False,
        "image_format": "png",
        "jpeg_quality": 90,
        "filename_template": "ScreenPilot_{date}_{time}_{mode}",
        "output_dir": "",
    },
    "watermark": {
        "enabled": False,
        "text": "{user} · {date} {time}",
        "position": "bottom-right",
        "opacity": 55,
        "size": 14,
    },
    "recording": {
        "format": "mp4",
        "fps": 30,
        "gif_fps": 12,
        "gif_max_width": 960,
        "quality": 23,
        "encoder": "auto",
        "microphone": "",
        "system_audio": "",
        "audio_offset_ms": 0,
        "show_cursor": True,
        "highlight_cursor": True,
        "click_effects": True,
        "zoom_factor": 2.0,
        "countdown": 3,
        "max_minutes": 60,
        "ffmpeg_path": "",
        "output_dir": "",
        "open_folder_after": False,
    },
    "workflow": {
        "after_capture": [
            {"id": "save"},
            {"id": "copy_image"},
        ],
        "stop_on_error": False,
    },
    "selector": {
        "colors": {},
        "thickness": 2,
        "dim": 47,
        "magnifier": True,
    },
    "ocr": {
        "language": "auto",
        "engine": "auto",
        "auto_copy": True,
        "show_window": True,
    },
    "editor": {
        "color": "#d13438",
        "width": 4,
    },
    "history": {
        "limit": 100,
    },
    "diagnostics": {
        "verbose_logging": False,
        "timings": True,
    },
    "privacy": {
        "telemetry": False,
        "allow_network": False,
    },
}

Listener = Callable[[str, Any], None]
Migration = Callable[[dict[str, Any]], dict[str, Any]]

# from_version -> function producing data for from_version + 1
MIGRATIONS: dict[int, Migration] = {}


def _get(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def _merge(defaults: dict[str, Any], stored: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Overlay ``stored`` on ``defaults`` with type validation."""
    result = copy.deepcopy(defaults)
    for key, value in stored.items():
        path = f"{prefix}{key}"
        if key not in defaults:
            result[key] = value  # keep unknown keys (newer version / plugins)
            continue
        default = defaults[key]
        if isinstance(default, dict):
            if isinstance(value, dict):
                result[key] = _merge(default, value, prefix=f"{path}.")
            else:
                log.warning("Setting %s should be an object; using default", path)
        elif _type_ok(default, value):
            result[key] = value
        else:
            log.warning("Setting %s has invalid type %s; using default", path, type(value).__name__)
    return result


def _type_ok(default: Any, value: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


class Settings:
    def __init__(self, path: Path | None, defaults: dict[str, Any] | None = None) -> None:
        self.path = path
        self._defaults = defaults if defaults is not None else DEFAULTS
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._data = copy.deepcopy(self._defaults)
        self.locked: dict[str, Any] = {}  # key -> value forced by administrator policy

    # -- persistence ----------------------------------------------------------
    def load(self) -> "Settings":
        if self.path is None or not self.path.exists():
            return self
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict):
                raise ValueError("root is not an object")
        except (OSError, ValueError) as exc:
            backup = self.path.with_suffix(".corrupt.json")
            log.error("Settings file unreadable (%s); backing up to %s and using defaults", exc, backup)
            try:
                os.replace(self.path, backup)
            except OSError:
                log.exception("Could not back up corrupt settings")
            return self
        stored = self._migrate(stored)
        with self._lock:
            self._data = _merge(self._defaults, stored)
        return self

    def _migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        version = data.get("schema_version", 1)
        if not isinstance(version, int):
            version = 1
        while version < SCHEMA_VERSION:
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise ConfigurationError(f"No settings migration from version {version}")
            data = migration(data)
            version += 1
            data["schema_version"] = version
            log.info("Migrated settings to schema %d", version)
        return data

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            text = json.dumps(self._data, indent=2, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- access ---------------------------------------------------------------
    def is_locked(self, key: str) -> bool:
        return key in self.locked

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.locked:
            return copy.deepcopy(self.locked[key])
        with self._lock:
            try:
                return copy.deepcopy(_get(self._data, key))
            except KeyError:
                return default

    def set(self, key: str, value: Any) -> None:
        if key in self.locked:
            raise ConfigurationError(f"{key} is locked by your administrator", message_key="error.locked")
        parts = key.split(".")
        try:
            expected = _get(self._defaults, key)
        except KeyError:
            expected = None
        if expected is not None and not isinstance(expected, dict) and not _type_ok(expected, value):
            raise ConfigurationError(
                f"Invalid type for {key}: expected {type(expected).__name__}, got {type(value).__name__}"
            )
        with self._lock:
            node = self._data
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            if node.get(parts[-1]) == value:
                return
            node[parts[-1]] = copy.deepcopy(value)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(key, value)
            except Exception:  # noqa: BLE001
                log.exception("Settings listener failed for %s", key)

    def reset(self, key: str) -> None:
        self.set(key, _get(self._defaults, key))

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)
