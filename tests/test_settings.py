import json
import tempfile
import unittest
from pathlib import Path

from screenpilot.core.errors import ConfigurationError
from screenpilot.settings import Settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config" / "settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_without_file(self):
        s = Settings(self.path).load()
        self.assertEqual(s.get("general.language"), "en")
        self.assertFalse(s.get("privacy.telemetry"))

    def test_roundtrip_and_atomic_save(self):
        s = Settings(self.path).load()
        s.set("general.language", "ro")
        s.save()
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["settings.json"])
        self.assertEqual(Settings(self.path).load().get("general.language"), "ro")

    def test_invalid_types_fall_back_to_default_and_unknown_keys_kept(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"capture": {"jpeg_quality": "high", "include_cursor": True}, "plugin_x": {"a": 1}}),
            encoding="utf-8",
        )
        with self.assertLogs("screenpilot.settings.store", level="WARNING"):
            s = Settings(self.path).load()
        self.assertEqual(s.get("capture.jpeg_quality"), 90)
        self.assertTrue(s.get("capture.include_cursor"))
        self.assertEqual(s.get("plugin_x.a"), 1)

    def test_corrupt_file_is_backed_up(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("screenpilot.settings.store", level="ERROR"):
            s = Settings(self.path).load()
        self.assertEqual(s.get("general.language"), "en")
        self.assertTrue(self.path.with_suffix(".corrupt.json").exists())

    def test_set_rejects_wrong_type(self):
        s = Settings(None)
        with self.assertRaises(ConfigurationError):
            s.set("capture.default_delay_seconds", "five")
        with self.assertRaises(ConfigurationError):
            s.set("general.tester_mode", 1)

    def test_listeners_fire_only_on_change(self):
        s = Settings(None)
        events = []
        s.add_listener(lambda k, v: events.append((k, v)))
        s.set("general.language", "ro")
        s.set("general.language", "ro")
        self.assertEqual(events, [("general.language", "ro")])

    def test_get_returns_copy(self):
        s = Settings(None)
        actions = s.get("workflow.after_capture")
        actions.append({"id": "x"})
        self.assertEqual(len(s.get("workflow.after_capture")), 2)


if __name__ == "__main__":
    unittest.main()
