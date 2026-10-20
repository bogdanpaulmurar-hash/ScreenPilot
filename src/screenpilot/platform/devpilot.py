"""DevPilot integration: features owned by the DevPilot hub when it started us.

DevPilot (the user's tray hub for all tools) starts managed tools with
``DEVPILOT_MANAGED=1`` and ``DEVPILOT_SUPPRESS=tray,keepawake,autostart``.
Started any other way, nothing changes.
"""

from __future__ import annotations

import os
from typing import Mapping

FEATURES = ("tray", "keepawake", "autostart")


def managed(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("DEVPILOT_MANAGED") == "1"


def suppressed(env: Mapping[str, str] | None = None) -> frozenset[str]:
    env = os.environ if env is None else env
    if not managed(env):
        return frozenset()
    return frozenset(p.strip().lower() for p in env.get("DEVPILOT_SUPPRESS", "").split(",") if p.strip())


def owns(feature: str, env: Mapping[str, str] | None = None) -> bool:
    """True when DevPilot started this process and owns ``feature`` (``tray``, ``keepawake``, ``autostart``)."""
    return feature.lower() in suppressed(env)
