"""Start with Windows, Start-menu shortcut and windowless launching.

ScreenPilot is distributed as Python (no ``.exe``), installed into its own
virtual environment so it never shares packages with the user's other Python
apps. It is launched with ``pythonw.exe`` — the console-less interpreter — so
only the tray icon is visible.

* Autostart: a value in ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
  (per user, no admin rights, visible in Task Manager › Startup apps).
* Start-menu shortcut: created through the ``WScript.Shell`` COM object from a
  PowerShell child process; paths are passed in environment variables, never
  interpolated into the script.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ScreenPilot"
APP_ID = "ScreenPilot.Desktop"
SHORTCUT_NAME = "ScreenPilot.lnk"


def windowless_python(executable: str | None = None) -> Path:
    """``pythonw.exe`` next to the running interpreter (falls back to it)."""
    exe = Path(executable or sys.executable)
    if exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return exe


def launch_command(*, autostart: bool = False, executable: str | None = None) -> str:
    args = [str(windowless_python(executable)), "-m", "screenpilot", "gui"]
    if autostart:
        args.append("--autostart")
    return subprocess.list2cmdline(args)


class Registry(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


class WinRegistry:
    """The current user's Run key."""

    def __init__(self) -> None:
        import winreg

        self._winreg = winreg

    def get(self, name: str) -> str | None:
        w = self._winreg
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY) as key:
                return str(w.QueryValueEx(key, name)[0])
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        w = self._winreg
        with w.CreateKeyEx(w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_SET_VALUE) as key:
            w.SetValueEx(key, name, 0, w.REG_SZ, value)

    def delete(self, name: str) -> None:
        w = self._winreg
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_SET_VALUE) as key:
                w.DeleteValue(key, name)
        except FileNotFoundError:
            pass


class Autostart:
    def __init__(self, registry: Registry | None = None, *, executable: str | None = None) -> None:
        if registry is None and sys.platform == "win32":
            registry = WinRegistry()
        self.registry = registry
        self.executable = executable

    @property
    def supported(self) -> bool:
        return self.registry is not None

    @property
    def command(self) -> str:
        return launch_command(autostart=True, executable=self.executable)

    def is_enabled(self) -> bool:
        return self.supported and self.registry.get(VALUE_NAME) is not None

    def is_current(self) -> bool:
        """Enabled and pointing at this installation (not a moved/old venv)."""
        return self.supported and self.registry.get(VALUE_NAME) == self.command

    def set_enabled(self, enabled: bool) -> None:
        if not self.supported:
            log.info("Autostart is only available on Windows")
            return
        if enabled:
            self.registry.set(VALUE_NAME, self.command)
            log.info("Autostart enabled: %s", self.command)
        else:
            self.registry.delete(VALUE_NAME)
            log.info("Autostart disabled")

    def sync(self, wanted: bool) -> None:
        """Make the registry match the setting (also repairs a moved installation)."""
        if not self.supported:
            return
        if wanted and not self.is_current():
            self.set_enabled(True)
        elif not wanted and self.is_enabled():
            self.set_enabled(False)


def set_app_user_model_id(app_id: str = APP_ID) -> bool:
    """Give the process its own taskbar identity.

    Without it, Windows groups every ``pythonw.exe`` app under one Python
    taskbar button with the Python icon; with it, ScreenPilot gets its own
    button and the ScreenPilot icon, separate from other Python apps.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id) == 0
    except (AttributeError, OSError):
        return False


def start_menu_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


_SHORTCUT_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SP_LNK);"
    "$s.TargetPath = $env:SP_TARGET; $s.Arguments = $env:SP_ARGS;"
    "$s.WorkingDirectory = $env:SP_WORKDIR; $s.IconLocation = $env:SP_ICON;"
    "$s.Description = 'ScreenPilot'; $s.Save()"
)


def create_shortcut(link: Path, icon: Path, *, executable: str | None = None) -> Path:
    if sys.platform != "win32":
        raise OSError("Shortcuts can only be created on Windows")
    link.parent.mkdir(parents=True, exist_ok=True)
    target = windowless_python(executable)
    env = dict(
        os.environ,
        SP_LNK=str(link),
        SP_TARGET=str(target),
        SP_ARGS="-m screenpilot gui",
        SP_WORKDIR=str(Path.home()),
        SP_ICON=f"{icon},0",
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _SHORTCUT_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0 or not link.exists():
        raise OSError(f"Creating the shortcut failed: {proc.stderr.strip()[-300:]}")
    log.info("Shortcut created: %s -> %s", link, target)
    return link
