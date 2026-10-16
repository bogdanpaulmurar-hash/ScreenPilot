import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from screenpilot.core.errors import CaptureError, ConfigurationError
from screenpilot.core.events import EventBus
from screenpilot.core.paths import ENV_HOME, PORTABLE_FLAG, resolve_paths
from screenpilot.core.services import ServiceRegistry


class EventBusTests(unittest.TestCase):
    def test_publish_reaches_named_and_wildcard_subscribers(self):
        bus = EventBus()
        seen = []
        bus.subscribe("capture.completed", lambda e: seen.append(("named", e.payload["id"])))
        bus.subscribe("*", lambda e: seen.append(("any", e.name)))
        bus.publish("capture.completed", id=7)
        self.assertEqual(seen, [("named", 7), ("any", "capture.completed")])

    def test_failing_handler_does_not_break_others(self):
        bus = EventBus()
        seen = []

        def boom(_):
            raise RuntimeError("handler bug")

        bus.subscribe("x", boom)
        bus.subscribe("x", lambda e: seen.append(e.name))
        with self.assertLogs("screenpilot.core.events", level="ERROR"):
            bus.publish("x")
        self.assertEqual(seen, ["x"])

    def test_cancel_subscription(self):
        bus = EventBus()
        seen = []
        sub = bus.subscribe("x", lambda e: seen.append(1))
        sub.cancel()
        sub.cancel()  # idempotent
        bus.publish("x")
        self.assertEqual(seen, [])


class ServiceRegistryTests(unittest.TestCase):
    def test_factory_is_lazy_and_singleton(self):
        calls = []

        class Foo:
            pass

        reg = ServiceRegistry()
        reg.register_factory(Foo, lambda r: calls.append(1) or Foo())
        self.assertEqual(calls, [])
        self.assertIs(reg.get(Foo), reg.get(Foo))
        self.assertEqual(calls, [1])

    def test_missing_service_raises(self):
        with self.assertRaises(ConfigurationError):
            ServiceRegistry().get(int)


class ErrorTests(unittest.TestCase):
    def test_message_key_and_params(self):
        err = CaptureError("BitBlt failed", message_key="error.capture.bitblt", code=5)
        self.assertEqual(err.message_key, "error.capture.bitblt")
        self.assertEqual(err.params, {"code": 5})
        self.assertEqual(CaptureError("x").message_key, "error.capture")


class PathTests(unittest.TestCase):
    def test_home_override_is_portable(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {ENV_HOME: tmp}):
            paths = resolve_paths()
            self.assertTrue(paths.portable)
            self.assertEqual(paths.settings_file, Path(tmp) / "config" / "settings.json")

    def test_portable_flag_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            (app_dir / PORTABLE_FLAG).touch()
            env = {k: v for k, v in os.environ.items() if k != ENV_HOME}
            with mock.patch.dict(os.environ, env, clear=True):
                paths = resolve_paths(app_dir).ensure()
            self.assertTrue(paths.portable)
            self.assertTrue(paths.captures_dir.is_dir())
            self.assertEqual(paths.config_dir, app_dir / "UserData" / "config")


if __name__ == "__main__":
    unittest.main()
