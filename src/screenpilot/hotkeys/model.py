"""Hotkey value object: parsing, canonical formatting and Win32 codes."""

from __future__ import annotations

from dataclasses import dataclass

from screenpilot.core.errors import HotkeyError

# Canonical modifier order used for display and comparison.
MODIFIERS = ("Ctrl", "Alt", "Shift", "Win")
_MODIFIER_ALIASES = {
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "ctl": "Ctrl",
    "alt": "Alt",
    "menu": "Alt",
    "shift": "Shift",
    "win": "Win",
    "windows": "Win",
    "super": "Win",
    "meta": "Win",
}
# RegisterHotKey modifier flags
MOD_FLAGS = {"Alt": 0x0001, "Ctrl": 0x0002, "Shift": 0x0004, "Win": 0x0008}
MOD_NOREPEAT = 0x4000

# Canonical key name -> virtual-key code.
VK_CODES: dict[str, int] = {}
VK_CODES.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
VK_CODES.update({str(d): 0x30 + d for d in range(10)})
VK_CODES.update({f"F{n}": 0x6F + n for n in range(1, 25)})
VK_CODES.update(
    {
        "PrintScreen": 0x2C,
        "Pause": 0x13,
        "ScrollLock": 0x91,
        "Insert": 0x2D,
        "Delete": 0x2E,
        "Home": 0x24,
        "End": 0x23,
        "PageUp": 0x21,
        "PageDown": 0x22,
        "Up": 0x26,
        "Down": 0x28,
        "Left": 0x25,
        "Right": 0x27,
        "Space": 0x20,
        "Enter": 0x0D,
        "Tab": 0x09,
        "Escape": 0x1B,
        "Backspace": 0x08,
    }
)
VK_CODES.update({f"Num{d}": 0x60 + d for d in range(10)})

_KEY_ALIASES = {name.lower(): name for name in VK_CODES}
_KEY_ALIASES.update(
    {
        "prtsc": "PrintScreen",
        "prtscn": "PrintScreen",
        "printscr": "PrintScreen",
        "print": "PrintScreen",
        "del": "Delete",
        "ins": "Insert",
        "pgup": "PageUp",
        "pgdn": "PageDown",
        "esc": "Escape",
        "return": "Enter",
    }
)


@dataclass(frozen=True, order=True)
class Hotkey:
    modifiers: tuple[str, ...]
    key: str

    @classmethod
    def parse(cls, text: str) -> "Hotkey":
        """Parse strings such as ``"ctrl+shift+s"`` or ``"Win + PrtSc"``."""
        if not text or not text.strip():
            raise HotkeyError("Empty hotkey")
        parts = [p.strip() for p in text.replace(" ", "").split("+")]
        if any(not p for p in parts):
            raise HotkeyError(f"Malformed hotkey: {text!r}")
        mods: set[str] = set()
        key: str | None = None
        for part in parts:
            low = part.lower()
            if low in _MODIFIER_ALIASES:
                mods.add(_MODIFIER_ALIASES[low])
            elif low in _KEY_ALIASES:
                if key is not None:
                    raise HotkeyError(f"Hotkey has more than one key: {text!r}")
                key = _KEY_ALIASES[low]
            else:
                raise HotkeyError(f"Unknown key {part!r} in {text!r}")
        if key is None:
            raise HotkeyError(f"Hotkey has no key, only modifiers: {text!r}")
        return cls(tuple(m for m in MODIFIERS if m in mods), key)

    def __str__(self) -> str:
        return "+".join((*self.modifiers, self.key))

    @property
    def vk(self) -> int:
        return VK_CODES[self.key]

    @property
    def win32_modifiers(self) -> int:
        flags = MOD_NOREPEAT
        for mod in self.modifiers:
            flags |= MOD_FLAGS[mod]
        return flags
