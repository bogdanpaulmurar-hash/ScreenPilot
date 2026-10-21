"""Capture bar button list from the setting (Qt-free)."""

import unittest

from screenpilot.ui.edge_items import DEFAULT_EDGE_BUTTONS, EDGE_ITEMS, SEPARATOR, edge_buttons


class EdgeButtonsTests(unittest.TestCase):
    def test_empty_or_invalid_means_default(self):
        self.assertEqual(edge_buttons([]), DEFAULT_EDGE_BUTTONS)
        self.assertEqual(edge_buttons(None), DEFAULT_EDGE_BUTTONS)
        self.assertEqual(edge_buttons(["nope", SEPARATOR]), DEFAULT_EDGE_BUTTONS)

    def test_cleanup_keeps_order_and_drops_duplicates_and_stray_separators(self):
        value = [SEPARATOR, "ocr.region", "capture.region", "ocr.region", SEPARATOR, SEPARATOR, "unknown", "hub", SEPARATOR]
        self.assertEqual(edge_buttons(value), ["ocr.region", "capture.region", SEPARATOR, "hub"])

    def test_every_default_is_in_the_catalogue(self):
        self.assertTrue(all(i == SEPARATOR or i in EDGE_ITEMS for i in DEFAULT_EDGE_BUTTONS))

    def test_catalogue_strings_exist_in_both_languages(self):
        import json
        from pathlib import Path

        root = Path(__file__).parents[1] / "src" / "screenpilot" / "i18n" / "locales"
        for lang in ("en", "ro"):
            catalog = json.loads((root / f"{lang}.json").read_text(encoding="utf-8"))
            for item_id, item in EDGE_ITEMS.items():
                for key in (item.label, item.tooltip):
                    if key:
                        self.assertIn(key, catalog, f"{lang}: {item_id}")


if __name__ == "__main__":
    unittest.main()
