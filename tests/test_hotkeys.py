import tempfile
import unittest
from pathlib import Path

from screenpilot.core.errors import HotkeyError
from screenpilot.hotkeys import ConflictKind, Hotkey, HotkeyManager, RegistrationStatus, describe_conflicts
from screenpilot.i18n import Translator


class FakeListener:
    def __init__(self, taken=()):
        self.taken = {Hotkey.parse(t) for t in taken}
        self.active = {}

    def register(self, binding_id, hotkey):
        if hotkey in self.taken or hotkey in self.active.values():
            return False
        self.active[binding_id] = hotkey
        return True

    def unregister(self, binding_id):
        self.active.pop(binding_id, None)


class HotkeyModelTests(unittest.TestCase):
    def test_parse_normalises_order_case_and_aliases(self):
        hk = Hotkey.parse("shift + ctrl + s")
        self.assertEqual(str(hk), "Ctrl+Shift+S")
        self.assertEqual(Hotkey.parse("CONTROL+SHIFT+s"), hk)
        self.assertEqual(str(Hotkey.parse("win+prtsc")), "Win+PrintScreen")
        self.assertEqual(str(Hotkey.parse("PrintScreen")), "PrintScreen")

    def test_win32_codes(self):
        hk = Hotkey.parse("Ctrl+Alt+F12")
        self.assertEqual(hk.vk, 0x7B)
        self.assertEqual(hk.win32_modifiers, 0x4000 | 0x0001 | 0x0002)

    def test_parse_errors(self):
        for bad in ("", "Ctrl+", "Ctrl+Shift", "Ctrl+A+B", "Ctrl+Banana"):
            with self.subTest(bad=bad), self.assertRaises(HotkeyError):
                Hotkey.parse(bad)


class HotkeyManagerTests(unittest.TestCase):
    def setUp(self):
        self.mgr = HotkeyManager()

    def test_defaults_have_no_conflicts(self):
        self.assertEqual(self.mgr.conflicts(), [])
        self.assertEqual(str(self.mgr.binding("capture.region")), "Ctrl+Shift+1")

    def test_duplicate_and_reserved_detection(self):
        self.mgr.assign("capture.window", "Ctrl+Shift+1")
        self.mgr.assign("ocr.region", "PrintScreen")
        kinds = {c.kind for c in self.mgr.conflicts()}
        self.assertEqual(kinds, {ConflictKind.DUPLICATE, ConflictKind.RESERVED})
        self.assertEqual(self.mgr.check("app.open_editor", "Ctrl+Shift+3")[0].kind, ConflictKind.DUPLICATE)
        self.assertEqual(self.mgr.check("capture.fullscreen", "Ctrl+Shift+3"), [])  # its own binding

    def test_localized_conflict_messages(self):
        self.mgr.assign("capture.window", "Ctrl+Shift+1")
        msgs = describe_conflicts(self.mgr.conflicts(), Translator("ro").t)
        self.assertEqual(
            msgs, ["Ctrl+Shift+1 este atribuită atât pentru „Capturează o zonă”, cât și pentru „Capturează fereastra activă”"]
        )

    def test_os_registration_diagnostics(self):
        listener = FakeListener(taken=["Ctrl+Shift+O"])
        self.mgr.assign("app.open_history", None)
        with self.assertLogs("screenpilot.hotkeys.manager", level="WARNING"):
            diag = self.mgr.register_all(listener)
        self.assertEqual(diag["capture.region"], RegistrationStatus.REGISTERED)
        self.assertEqual(diag["ocr.region"], RegistrationStatus.FAILED)
        self.assertEqual(diag["app.open_history"], RegistrationStatus.UNASSIGNED)
        self.assertEqual([c.kind for c in self.mgr.conflicts()], [ConflictKind.OS_TAKEN])
        # reassigning re-registers immediately
        self.mgr.assign("ocr.region", "Ctrl+Alt+O")
        self.assertEqual(self.mgr.diagnostics()["ocr.region"], RegistrationStatus.REGISTERED)
        bid = next(k for k, v in listener.active.items() if v == Hotkey.parse("Ctrl+Alt+O"))
        self.assertEqual(self.mgr.action_for_binding_id(bid), "ocr.region")

    def test_search_by_id_label_and_hotkey(self):
        tr = Translator("ro")
        self.assertEqual(self.mgr.search("ctrl+shift+r"), ["capture.repeat_last"])
        self.assertIn("capture.window", self.mgr.search("fereastra", label=lambda a: tr.t(f"action.{a}")))
        self.assertEqual(len(self.mgr.search("")), len(self.mgr.actions))

    def test_profile_roundtrip_and_validation(self):
        self.mgr.assign("capture.region", "Ctrl+Alt+Z")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hotkeys.json"
            self.mgr.save(path)
            other = HotkeyManager()
            self.assertEqual(other.load(path), [])
            self.assertEqual(str(other.binding("capture.region")), "Ctrl+Alt+Z")
        warnings = self.mgr.import_profile(
            {"format": "screenpilot.hotkeys", "version": 1, "bindings": {"nope": "A", "capture.window": "Ctrl+Bad"}}
        )
        self.assertEqual(len(warnings), 2)
        with self.assertRaises(HotkeyError):
            self.mgr.import_profile({"format": "other"})
        with self.assertRaises(HotkeyError):
            self.mgr.import_profile({"format": "screenpilot.hotkeys", "version": 99, "bindings": {}})


if __name__ == "__main__":
    unittest.main()
