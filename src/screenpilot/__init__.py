"""ScreenPilot - screenshot, screen recording and documentation platform.

The package is split into a headless core (capture, settings, i18n, hotkeys,
workflow) and an optional Qt user interface. Automation clients such as
Autopilot should depend only on the public service APIs exposed by
:mod:`screenpilot.app.container`.
"""

__all__ = ["__version__", "APP_NAME"]

__version__ = "0.5.2"
APP_NAME = "ScreenPilot"
