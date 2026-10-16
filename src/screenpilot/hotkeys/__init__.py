"""Hotkey management: global hotkeys, conflicts, diagnostics, profiles."""

from screenpilot.hotkeys.manager import (
    DEFAULT_BINDINGS,
    Conflict,
    ConflictKind,
    HotkeyListener,
    HotkeyManager,
    RegistrationStatus,
    describe_conflicts,
)
from screenpilot.hotkeys.model import Hotkey

__all__ = [
    "DEFAULT_BINDINGS",
    "Conflict",
    "ConflictKind",
    "Hotkey",
    "HotkeyListener",
    "HotkeyManager",
    "RegistrationStatus",
    "describe_conflicts",
]
