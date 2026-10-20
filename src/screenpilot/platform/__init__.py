"""OS integration services (clipboard, shell) behind small interfaces."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Protocol

from screenpilot.capture.image import CapturedImage
from screenpilot.core.errors import PlatformNotSupportedError


class Clipboard(Protocol):
    def set_text(self, text: str) -> None: ...

    def set_image(self, image: CapturedImage) -> None: ...


class Shell(Protocol):
    def reveal(self, path: Path) -> None:
        """Open the containing folder with ``path`` selected."""


class UnsupportedClipboard:
    def set_text(self, text: str) -> None:
        raise PlatformNotSupportedError("Clipboard is not supported on this platform")

    def set_image(self, image: CapturedImage) -> None:
        raise PlatformNotSupportedError("Clipboard is not supported on this platform")


class DefaultShell:
    def reveal(self, path: Path) -> None:
        path = Path(path).resolve()
        if sys.platform == "win32":
            from screenpilot.platform.win32_window import reveal_in_explorer

            if not reveal_in_explorer(path):
                raise OSError(f"Could not open Explorer for {path}")
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])  # noqa: S603,S607
        else:
            folder = path if path.is_dir() else path.parent
            subprocess.Popen(["xdg-open", str(folder)])  # noqa: S603,S607


def default_clipboard() -> Clipboard:
    if sys.platform == "win32":
        from screenpilot.platform.win32_clipboard import Win32Clipboard

        return Win32Clipboard()
    return UnsupportedClipboard()


__all__ = ["Clipboard", "Shell", "DefaultShell", "UnsupportedClipboard", "default_clipboard"]
