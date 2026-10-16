"""In-process publish/subscribe event bus.

Modules communicate through events instead of importing each other, which keeps
the engines loosely coupled and lets plugins / Autopilot observe what happens
(e.g. ``capture.completed``) without patching code.

Handlers run synchronously on the publisher's thread. A failing handler is
logged and never breaks the publisher or other handlers. UI code that must run
on the GUI thread is responsible for marshalling the call itself.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

Handler = Callable[["Event"], None]

WILDCARD = "*"


@dataclass(frozen=True)
class Event:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Subscription:
    """Handle returned by :meth:`EventBus.subscribe`; call :meth:`cancel` to stop."""

    def __init__(self, bus: "EventBus", name: str, handler: Handler) -> None:
        self._bus = bus
        self.name = name
        self.handler = handler
        self.active = True

    def cancel(self) -> None:
        if self.active:
            self._bus._remove(self)
            self.active = False


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, list[Subscription]] = {}

    def subscribe(self, name: str, handler: Handler) -> Subscription:
        """Subscribe to an event name, or ``"*"`` for every event."""
        sub = Subscription(self, name, handler)
        with self._lock:
            self._subs.setdefault(name, []).append(sub)
        return sub

    def publish(self, name: str, **payload: Any) -> Event:
        event = Event(name=name, payload=payload)
        with self._lock:
            targets = list(self._subs.get(name, ())) + list(self._subs.get(WILDCARD, ()))
        for sub in targets:
            try:
                sub.handler(event)
            except Exception:  # noqa: BLE001 - handlers must never break publishers
                log.exception("Event handler failed for %s", name)
        return event

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            subs = self._subs.get(sub.name)
            if subs and sub in subs:
                subs.remove(sub)
