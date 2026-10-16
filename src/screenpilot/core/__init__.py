"""Cross-cutting infrastructure: events, service registry, logging, paths."""

from screenpilot.core.errors import ScreenPilotError, CaptureError, ConfigurationError, PlatformNotSupportedError
from screenpilot.core.events import EventBus, Event
from screenpilot.core.services import ServiceRegistry

__all__ = [
    "ScreenPilotError",
    "CaptureError",
    "ConfigurationError",
    "PlatformNotSupportedError",
    "EventBus",
    "Event",
    "ServiceRegistry",
]
