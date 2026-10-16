import json
import tempfile
import unittest
from pathlib import Path

from screenpilot.i18n.translator import BUILTIN_LOCALES_DIR, META_KEY, Translator, placeholders


class LocaleCompletenessTests(unittest.TestCase):
    """Guards the rule that every UI string exists in every shipped language."""

    def setUp(self):
        self.catalogs = {
            p.stem: json.loads(p.read_text(encoding="utf-8")) for p in BUILTIN_LOCALES_DIR.glob("*.json")
        }

    def test_english_and_romanian_shipped(self):
        self.assertIn("en", self.catalogs)
        self.assertIn("ro", self.catalogs)

    def test_all_languages_have_same_keys_and_placeholders(self):
        reference = {k: v for k, v in self.catalogs["en"].items() if k != META_KEY}
        for code, catalog in self.catalogs.items():
            keys = {k for k in catalog if k != META_KEY}
            self.assertEqual(set(reference) - keys, set(), f"{code} is missing keys")
            self.assertEqual(keys - set(reference), set(), f"{code} has unknown keys")
            for key, text in reference.items():
                self.assertEqual(placeholders(text), placeholders(catalog[key]), f"{code}:{key} placeholders differ")
            self.assertIn("name", catalog[META_KEY])


class TranslatorTests(unittest.TestCase):
    def test_lookup_and_formatting(self):
        tr = Translator("ro")
        self.assertEqual(tr.t("settings.title"), "Setări")
        self.assertEqual(tr.t("capture.delay.countdown", seconds=3), "Captură în 3...")

    def test_live_switch_notifies_listeners(self):
        tr = Translator("en")
        changes = []
        remove = tr.add_listener(changes.append)
        tr.set_language("ro")
        tr.set_language("ro")  # no change -> no notification
        self.assertEqual(changes, ["ro"])
        self.assertEqual(tr("settings.title"), "Setări")
        remove()
        tr.set_language("en")
        self.assertEqual(changes, ["ro"])

    def test_unknown_language_falls_back_to_english(self):
        with self.assertLogs("screenpilot.i18n.translator", level="WARNING"):
            tr = Translator("xx")
        self.assertEqual(tr.language, "en")

    def test_missing_key_returns_key_and_missing_param_kept(self):
        tr = Translator("en")
        self.assertEqual(tr.t("does.not.exist"), "does.not.exist")
        self.assertEqual(tr.t("capture.delay.countdown"), "Capturing in {seconds}...")

    def test_extra_locale_dir_adds_language_and_falls_back_per_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "de.json").write_text(
                json.dumps({"_meta": {"name": "Deutsch"}, "settings.title": "Einstellungen"}), encoding="utf-8"
            )
            tr = Translator("de", locale_dirs=[Path(tmp)])
            self.assertEqual(tr.available_languages()["de"], "Deutsch")
            self.assertEqual(tr.t("settings.title"), "Einstellungen")
            self.assertEqual(tr.t("hotkeys.title"), "Hotkeys")  # English fallback

    def test_available_languages(self):
        langs = Translator().available_languages()
        self.assertEqual(langs["en"], "English")
        self.assertEqual(langs["ro"], "Română")


if __name__ == "__main__":
    unittest.main()
