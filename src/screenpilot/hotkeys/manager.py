"""Hotkey bindings: defaults, conflict detection, diagnostics and profiles.

The manager is OS-independent. Actual system-wide registration is delegated
to a :class:`HotkeyListener` (Win32 implementation in ``win32_listener``),
whose per-binding success/failure is recorded for the diagnostics view.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Protocol

from screenpilot.core.errors import HotkeyError
from screenpilot.hotkeys.model import Hotkey

log = logging.getLogger(__name__)

PROFILE_FORMAT = "screenpilot.hotkeys"
PROFILE_VERSION = 1

# action id -> default hotkey (None = unassigned). Action ids are stable API.
DEFAULT_BINDINGS: dict[str, str | None] = {
    "capture.region": "Ctrl+Shift+1",
    "capture.window": "Ctrl+Shift+2",
    "capture.fullscreen": "Ctrl+Shift+3",
    "capture.all_monitors": "Ctrl+Shift+4",
    "capture.delayed": "Ctrl+Shift+5",
    "capture.scrolling": "Ctrl+Shift+6",
    "capture.repeat_last": "Ctrl+Shift+R",
    "capture.edit": "Ctrl+Shift+E",
    "capture.steps": "Ctrl+Shift+N",
    "capture.copy": None,
    "capture.pin": None,
    "ocr.region": "Ctrl+Shift+O",
    "app.open_editor": None,
    "tools.color": "Ctrl+Shift+C",
    "tools.measure": "Ctrl+Shift+M",
    "capture.interval": None,
    "procedure.toggle": "Ctrl+Shift+P",
    "app.open_history": "Ctrl+Shift+H",
    "record.video": "Ctrl+Shift+9",
    "record.gif": "Ctrl+Shift+8",
    "record.pause": None,
    "record.zoom": None,
    "record.draw": None,  # pen on/off while recording (assign in Settings › Hotkeys)
}

# Combinations Windows reserves or commonly uses; value is an i18n reason key.
RESERVED: dict[str, str] = {
    "Win+L": "hotkeys.reserved.lock",
    "Ctrl+Alt+Delete": "hotkeys.reserved.security",
    "Alt+Tab": "hotkeys.reserved.task_switch",
    "Alt+F4": "hotkeys.reserved.close_window",
    "Shift+Win+S": "hotkeys.reserved.snipping_tool",
    "PrintScreen": "hotkeys.reserved.snipping_tool",
    "Ctrl+Escape": "hotkeys.reserved.start_menu",
}


class ConflictKind(str, Enum):
    DUPLICATE = "duplicate"
    RESERVED = "reserved"
    OS_TAKEN = "os_taken"


@dataclass(frozen=True)
class Conflict:
    kind: ConflictKind
    hotkey: Hotkey
    actions: tuple[str, ...]
    reason_key: str = ""


class RegistrationStatus(str, Enum):
    REGISTERED = "registered"
    FAILED = "failed"
    UNASSIGNED = "unassigned"
    PENDING = "pending"


class HotkeyListener(Protocol):
    def register(self, binding_id: int, hotkey: Hotkey) -> bool: ...

    def unregister(self, binding_id: int) -> None: ...


class HotkeyManager:
    def __init__(self, defaults: dict[str, str | None] | None = None) -> None:
        self._defaults = dict(DEFAULT_BINDINGS if defaults is None else defaults)
        self._bindings: dict[str, Hotkey | None] = {}
        self._status: dict[str, RegistrationStatus] = {}
        self._listener: HotkeyListener | None = None
        self._ids: dict[str, int] = {}
        self.reset_to_defaults()

    # -- bindings -------------------------------------------------------------
    @property
    def actions(self) -> list[str]:
        return list(self._bindings)

    def binding(self, action: str) -> Hotkey | None:
        return self._bindings.get(action)

    def bindings(self) -> dict[str, Hotkey | None]:
        return dict(self._bindings)

    def assign(self, action: str, hotkey: Hotkey | str | None) -> None:
        if action not in self._bindings:
            raise HotkeyError(f"Unknown action: {action}")
        if isinstance(hotkey, str):
            hotkey = Hotkey.parse(hotkey)
        self._bindings[action] = hotkey
        if self._listener is not None:
            self._register_one(action)

    def reset_to_defaults(self) -> None:
        self._bindings = {a: (Hotkey.parse(h) if h else None) for a, h in self._defaults.items()}
        self._status = {a: RegistrationStatus.PENDING for a in self._bindings}
        if self._listener is not None:
            self.register_all(self._listener)

    def action_for(self, hotkey: Hotkey) -> str | None:
        for action, bound in self._bindings.items():
            if bound == hotkey:
                return action
        return None

    # -- conflicts ------------------------------------------------------------
    def conflicts(self) -> list[Conflict]:
        result: list[Conflict] = []
        by_key: dict[Hotkey, list[str]] = {}
        for action, hotkey in self._bindings.items():
            if hotkey is not None:
                by_key.setdefault(hotkey, []).append(action)
        for hotkey, actions in by_key.items():
            if len(actions) > 1:
                result.append(Conflict(ConflictKind.DUPLICATE, hotkey, tuple(actions)))
            reason = RESERVED.get(str(hotkey))
            if reason:
                result.append(Conflict(ConflictKind.RESERVED, hotkey, tuple(actions), reason))
        for action, status in self._status.items():
            hotkey = self._bindings.get(action)
            if status is RegistrationStatus.FAILED and hotkey is not None:
                if not any(c.kind is ConflictKind.DUPLICATE and action in c.actions for c in result):
                    result.append(Conflict(ConflictKind.OS_TAKEN, hotkey, (action,)))
        return result

    def check(self, action: str, hotkey: Hotkey | str) -> list[Conflict]:
        """Conflicts that assigning ``hotkey`` to ``action`` would create (for live UI feedback)."""
        if isinstance(hotkey, str):
            hotkey = Hotkey.parse(hotkey)
        found = []
        other = self.action_for(hotkey)
        if other is not None and other != action:
            found.append(Conflict(ConflictKind.DUPLICATE, hotkey, (other, action)))
        reason = RESERVED.get(str(hotkey))
        if reason:
            found.append(Conflict(ConflictKind.RESERVED, hotkey, (action,), reason))
        return found

    # -- search ---------------------------------------------------------------
    def search(self, query: str, label: Callable[[str], str] = lambda a: a) -> list[str]:
        """Actions whose id, localized label or hotkey text contain ``query``."""
        q = query.strip().lower().replace(" ", "")
        if not q:
            return self.actions
        result = []
        for action, hotkey in self._bindings.items():
            haystacks = (action.lower(), label(action).lower().replace(" ", ""), str(hotkey or "").lower())
            if any(q in h for h in haystacks):
                result.append(action)
        return result

    # -- OS registration and diagnostics -------------------------------------
    def register_all(self, listener: HotkeyListener) -> dict[str, RegistrationStatus]:
        self._listener = listener
        for action in self._bindings:
            self._register_one(action)
        return self.diagnostics()

    def _register_one(self, action: str) -> None:
        assert self._listener is not None
        binding_id = self._ids.setdefault(action, len(self._ids) + 1)
        self._listener.unregister(binding_id)
        hotkey = self._bindings[action]
        if hotkey is None:
            self._status[action] = RegistrationStatus.UNASSIGNED
            return
        try:
            ok = self._listener.register(binding_id, hotkey)
        except Exception:  # noqa: BLE001
            log.exception("Registering %s for %s raised", hotkey, action)
            ok = False
        self._status[action] = RegistrationStatus.REGISTERED if ok else RegistrationStatus.FAILED
        if not ok:
            log.warning("Hotkey %s for %s could not be registered", hotkey, action)

    def action_for_binding_id(self, binding_id: int) -> str | None:
        for action, bid in self._ids.items():
            if bid == binding_id:
                return action
        return None

    def diagnostics(self) -> dict[str, RegistrationStatus]:
        return dict(self._status)

    # -- profiles -------------------------------------------------------------
    def export_profile(self, name: str = "default") -> dict:
        return {
            "format": PROFILE_FORMAT,
            "version": PROFILE_VERSION,
            "name": name,
            "bindings": {a: (str(h) if h else None) for a, h in self._bindings.items()},
        }

    def import_profile(self, profile: dict) -> list[str]:
        """Apply a profile; returns warnings for entries that were skipped."""
        if profile.get("format") != PROFILE_FORMAT:
            raise HotkeyError("Not a ScreenPilot hotkey profile")
        if not isinstance(profile.get("version"), int) or profile["version"] > PROFILE_VERSION:
            raise HotkeyError("Unsupported hotkey profile version")
        bindings = profile.get("bindings")
        if not isinstance(bindings, dict):
            raise HotkeyError("Hotkey profile has no bindings")
        warnings = []
        for action, text in bindings.items():
            if action not in self._bindings:
                warnings.append(f"Unknown action skipped: {action}")
                continue
            try:
                self.assign(action, text if text else None)
            except HotkeyError as exc:
                warnings.append(f"{action}: {exc}")
        return warnings

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.export_profile(), indent=2), encoding="utf-8")

    def load(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            return self.import_profile(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, HotkeyError) as exc:
            log.error("Could not load hotkeys from %s: %s", path, exc)
            return [str(exc)]


def describe_conflicts(conflicts: Iterable[Conflict], t: Callable[..., str]) -> list[str]:
    """Localized, human-readable conflict messages."""
    messages = []
    for c in conflicts:
        labels = [t(f"action.{a}") for a in c.actions]
        if c.kind is ConflictKind.DUPLICATE:
            messages.append(t("hotkeys.conflict.duplicate", hotkey=c.hotkey, first=labels[0], second=labels[1]))
        elif c.kind is ConflictKind.RESERVED:
            messages.append(t("hotkeys.conflict.reserved", hotkey=c.hotkey, reason=t(c.reason_key)))
        else:
            messages.append(t("hotkeys.conflict.os_taken", hotkey=c.hotkey))
    return messages
