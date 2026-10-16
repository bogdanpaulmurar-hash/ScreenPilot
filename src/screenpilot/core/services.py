"""Minimal service registry (dependency container).

Services are registered by interface type and resolved lazily. This is the
single integration point for the UI, the CLI, plugins and external automation
(Autopilot): they ask the registry for ``CaptureService``, ``Translator`` etc.
instead of constructing engines themselves.
"""

from __future__ import annotations

import threading
from typing import Callable, TypeVar, cast

from screenpilot.core.errors import ConfigurationError

T = TypeVar("T")


class ServiceRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instances: dict[type, object] = {}
        self._factories: dict[type, Callable[["ServiceRegistry"], object]] = {}

    def register_instance(self, key: type[T], instance: T) -> None:
        with self._lock:
            self._instances[key] = instance

    def register_factory(self, key: type[T], factory: Callable[["ServiceRegistry"], T]) -> None:
        """Register a factory; the instance is created on first :meth:`get`."""
        with self._lock:
            self._factories[key] = factory
            self._instances.pop(key, None)

    def get(self, key: type[T]) -> T:
        with self._lock:
            if key in self._instances:
                return cast(T, self._instances[key])
            factory = self._factories.get(key)
            if factory is None:
                raise ConfigurationError(f"Service not registered: {key.__name__}")
            instance = factory(self)
            self._instances[key] = instance
            return cast(T, instance)

    def has(self, key: type) -> bool:
        with self._lock:
            return key in self._instances or key in self._factories
