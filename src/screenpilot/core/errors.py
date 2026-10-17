"""Exception hierarchy.

Every error raised by ScreenPilot code derives from :class:`ScreenPilotError`
so callers (UI, CLI, Autopilot) can handle failures uniformly. Errors carry an
optional ``message_key`` that the UI translates for the user, while ``str(err)``
stays in English for logs.
"""

from __future__ import annotations


class ScreenPilotError(Exception):
    """Base class for all ScreenPilot errors."""

    message_key: str = "error.generic"

    def __init__(self, message: str, *, message_key: str | None = None, **params: object) -> None:
        super().__init__(message)
        if message_key is not None:
            self.message_key = message_key
        self.params = params


class ConfigurationError(ScreenPilotError):
    message_key = "error.configuration"


class CaptureError(ScreenPilotError):
    message_key = "error.capture"


class PlatformNotSupportedError(ScreenPilotError):
    message_key = "error.platform_not_supported"


class HotkeyError(ScreenPilotError):
    message_key = "error.hotkey"


class WorkflowError(ScreenPilotError):
    message_key = "error.workflow"


class RecordingError(ScreenPilotError):
    message_key = "error.recording"
