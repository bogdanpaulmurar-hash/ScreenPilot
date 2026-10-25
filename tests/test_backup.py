import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from screenpilot.core import backup
from screenpilot.core.paths import AppPaths


def make_paths(root: Path) -> AppPaths:
    return AppPaths.under(root, portable=True).ensure()


def seed(paths: AppPaths) -> None:
    """A user profile with settings, hotkeys, history, a thumbnail and a procedure."""
    paths.settings_file.write_text(json.dumps({"schema_version": 3, "general": {"theme": "dark"}}), encoding="utf-8")
    paths.hotkeys_file.write_text(
        json.dumps({"format": "screenpilot.hotkeys", "version": 1, "bindings": {"capture.region": "Ctrl+Shift+1"}}),
        encoding="utf-8",
    )
    thumbs = paths.data_dir / "thumbnails"
    thumbs.mkdir()
    (thumbs / "abc.png").write_bytes(b"png")
    capture = paths.captures_dir / "shot.png"
    capture.write_bytes(b"capture")
    history = {
        "version": 1,
        "entries": [
            {
                "id": "abc",
                "path": str(capture),
                "mode": "region",
                "width": 10,
                "height": 10,
                "timestamp": "2026-10-25T05:00:00+00:00",
                "thumbnail": str(thumbs / "abc.png"),
            }
        ],
    }
    (paths.data_dir / "history.json").write_text(json.dumps(history), encoding="utf-8")
    proc = paths.data_dir / "procedures" / "20261025-070000-aaaaaa"
    proc.mkdir(parents=True)
    (proc / "procedure.json").write_text("{}", encoding="utf-8")
    (proc / "step-001.png").write_bytes(b"step")
    (paths.data_dir / "drag").mkdir()
    (paths.data_dir / "drag" / "temp.png").write_bytes(b"temp")
    (paths.log_dir / "screenpilot.log").write_text("log", encoding="utf-8")


class CreateBackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.paths = make_paths(self.root / "profile")
        self.folder = self.root / "backups"
        seed(self.paths)

    def tearDown(self):
        self._tmp.cleanup()

    def test_zip_holds_settings_and_context_but_not_captures(self):
        result = backup.create_backup(self.paths, self.folder, now=datetime(2026, 10, 25, 7, 15, 0))
        self.assertEqual(result.path.name, "ScreenPilot-backup-20261025-071500.zip")
        with zipfile.ZipFile(result.path) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))
        self.assertEqual(
            names,
            {
                "manifest.json",
                "config/settings.json",
                "config/hotkeys.json",
                "data/history.json",
                "data/thumbnails/abc.png",
                "data/procedures/20261025-070000-aaaaaa/procedure.json",
                "data/procedures/20261025-070000-aaaaaa/step-001.png",
            },
        )
        self.assertEqual(manifest["format"], backup.FORMAT)
        self.assertEqual(manifest["contents"]["history_entries"], 1)
        self.assertEqual(manifest["contents"]["procedures"], 1)
        self.assertEqual(manifest["home"], str(Path.home()))
        self.assertFalse(any(n.endswith("shot.png") or "drag" in n or "log" in n for n in names))
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_live_settings_win_over_the_file(self):
        result = backup.create_backup(self.paths, self.folder, settings_data={"general": {"theme": "light"}})
        contents = backup.read_backup(result.path)
        self.assertEqual(contents.settings, {"general": {"theme": "light"}})
        self.assertEqual(contents.hotkeys["bindings"]["capture.region"], "Ctrl+Shift+1")
        self.assertEqual(contents.thumbnails, {"abc.png": b"png"})
        self.assertEqual(contents.procedures["20261025-070000-aaaaaa"]["step-001.png"], b"step")

    def test_empty_profile_still_makes_a_backup(self):
        empty = make_paths(self.root / "empty")
        result = backup.create_backup(empty, self.folder)
        contents = backup.read_backup(result.path)
        self.assertIsNone(contents.settings)
        self.assertEqual(contents.procedures, {})

    def test_same_second_gets_a_new_name(self):
        now = datetime(2026, 10, 25, 7, 15, 0)
        first = backup.create_backup(self.paths, self.folder, now=now).path
        second = backup.create_backup(self.paths, self.folder, now=now).path
        self.assertNotEqual(first, second)
        self.assertEqual(second.name, "ScreenPilot-backup-20261025-071500-2.zip")
        self.assertEqual(backup.list_backups(self.folder), [second, first])

    def test_rotation_keeps_newest_and_ignores_other_files(self):
        (self.folder).mkdir()
        other = self.folder / "my-notes.zip"
        other.write_bytes(b"x")
        safety = backup.create_backup(self.paths, self.folder, safety=True, now=datetime(2026, 1, 1, 0, 0, 0)).path
        made = [
            backup.create_backup(self.paths, self.folder, keep=3, now=datetime(2026, 10, day, 8, 0, 0)).path
            for day in range(1, 6)
        ]
        self.assertEqual(backup.list_backups(self.folder), list(reversed(made[2:])))
        self.assertTrue(other.exists())
        self.assertTrue(safety.exists())
        self.assertTrue(safety.name.endswith("-before-restore.zip"))

    def test_unwritable_folder_raises_backup_error(self):
        blocker = self.root / "file"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(backup.BackupError) as ctx:
            backup.create_backup(self.paths, blocker / "sub")
        self.assertEqual(ctx.exception.message_key, "backup.error.folder")


class ReadBackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _zip(self, members: dict[str, str]) -> Path:
        path = self.root / "some.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, text in members.items():
                zf.writestr(name, text)
        return path

    def test_random_zip_is_rejected(self):
        with self.assertRaises(backup.BackupError) as ctx:
            backup.read_backup(self._zip({"readme.txt": "hi"}))
        self.assertEqual(ctx.exception.message_key, "backup.error.not_backup")

    def test_not_a_zip_is_rejected(self):
        path = self.root / "x.zip"
        path.write_text("not a zip", encoding="utf-8")
        with self.assertRaises(backup.BackupError):
            backup.read_backup(path)

    def test_newer_version_is_rejected(self):
        manifest = json.dumps({"format": backup.FORMAT, "version": backup.VERSION + 1})
        with self.assertRaises(backup.BackupError) as ctx:
            backup.read_backup(self._zip({"manifest.json": manifest}))
        self.assertEqual(ctx.exception.message_key, "backup.error.newer")

    def test_unsafe_member_names_are_ignored(self):
        manifest = json.dumps({"format": backup.FORMAT, "version": 1})
        contents = backup.read_backup(
            self._zip({"manifest.json": manifest, "data/procedures/../../evil/x.txt": "x", "data/thumbnails/ok.png": "p"})
        )
        self.assertEqual(contents.procedures, {})
        self.assertEqual(contents.thumbnails, {"ok.png": b"p"})


class ScheduleTests(unittest.TestCase):
    def test_due(self):
        now = datetime(2026, 10, 25, 9, 0)
        self.assertTrue(backup.is_due("daily", "", now))
        self.assertTrue(backup.is_due("daily", "2026-10-24T23:59:00", now))
        self.assertFalse(backup.is_due("daily", "2026-10-25T07:00:00", now))
        self.assertTrue(backup.is_due("daily", "garbage", now))
        self.assertTrue(backup.is_due("every_start", "2026-10-25T08:59:00", now))
        self.assertFalse(backup.is_due("manual", "", now))

    def test_folders(self):
        paths = AppPaths.under(Path("/x/UserData"), portable=True)
        self.assertEqual(backup.backup_folder(paths, ""), Path("/x/UserData/Backups"))
        self.assertEqual(backup.backup_folder(paths, "/tmp/b"), Path("/tmp/b"))
        installed = AppPaths.under(Path("/x/UserData"), portable=False)
        self.assertEqual(backup.default_folder(installed), Path.home() / "Documents" / "ScreenPilot Backups")
        self.assertTrue(backup.is_cloud_folder(Path(r"G:\My Drive\Backups")))
        self.assertTrue(backup.is_cloud_folder(Path(r"C:\Users\me\OneDrive - Work\Backups")))
        self.assertFalse(backup.is_cloud_folder(Path(r"D:\Backups")))


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.old = make_paths(self.root / "old" / "profile")
        seed(self.old)
        settings = {
            "schema_version": 3,
            "general": {"theme": "dark", "start_with_windows": True},
            "capture": {"output_dir": str(self.root / "old" / "home" / "Pictures" / "Shots")},
            "recording": {"ffmpeg_path": str(self.root / "old" / "home" / "ffmpeg.exe")},
            "backup": {"folder": "X:/old-backups", "keep": 3},
        }
        made = backup.create_backup(self.old, self.root / "b", settings_data=settings)
        self.contents = backup.read_backup(made.path)
        self.contents.manifest["home"] = str(self.root / "old" / "home")
        self.new = make_paths(self.root / "new" / "profile")
        self.home = self.root / "new" / "home"

    def tearDown(self):
        self._tmp.cleanup()

    def _plan(self, **kw):
        current = {"general": {"theme": "light", "start_with_windows": False}, "backup": {"folder": "D:/mine", "keep": 7}}
        kw.setdefault("current_settings", current)
        return backup.plan_restore(self.contents, self.new, home=self.home, **kw)

    def test_preview_lists_changes_and_keeps_local_keys(self):
        plan = self._plan(current_hotkeys={"bindings": {"capture.region": "Ctrl+Shift+Z"}})
        self.assertIn("general.theme", plan.settings_changes)
        self.assertNotIn("general.start_with_windows", plan.settings_changes)
        self.assertFalse(any(k.startswith("backup.") for k in plan.settings_changes))
        self.assertEqual(plan.settings["backup"]["folder"], "D:/mine")
        self.assertEqual(plan.hotkey_changes, ["capture.region"])
        self.assertEqual(plan.procedures_new, ["20261025-070000-aaaaaa"])
        self.assertTrue(plan.has_changes)

    def test_paths_from_another_user_folder_are_adapted(self):
        plan = self._plan()
        self.assertEqual(Path(plan.settings["capture"]["output_dir"]), self.home / "Pictures" / "Shots")
        # ffmpeg is not at the adapted place on this PC: the local value stays
        self.assertEqual(plan.settings["recording"]["ffmpeg_path"], "")
        # the capture lived in the old captures folder; it maps to this PC's captures folder
        moved = self.new.captures_dir / "shot.png"
        self.assertEqual(plan.history_missing, [str(moved)])
        moved.write_bytes(b"capture")
        plan = self._plan()
        self.assertEqual(plan.history_missing, [])
        self.assertEqual(Path(plan.history_added[0]["path"]), moved)
        self.assertEqual(Path(plan.history_added[0]["thumbnail"]), self.new.data_dir / "thumbnails" / "abc.png")

    def test_adapt_paths_off_keeps_paths(self):
        plan = self._plan(adapt_paths=False)
        self.assertEqual(Path(plan.settings["capture"]["output_dir"]), self.root / "old" / "home" / "Pictures" / "Shots")

    def test_apply_writes_files(self):
        (self.new.captures_dir / "shot.png").write_bytes(b"capture")
        plan = self._plan(current_hotkeys={"bindings": {}})
        backup.apply_restore(plan, self.new)
        settings = json.loads(self.new.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(settings["general"]["theme"], "dark")
        self.assertFalse(settings["general"]["start_with_windows"])
        history = json.loads((self.new.data_dir / "history.json").read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in history["entries"]], ["abc"])
        self.assertEqual((self.new.data_dir / "thumbnails" / "abc.png").read_bytes(), b"png")
        step = self.new.data_dir / "procedures" / "20261025-070000-aaaaaa" / "step-001.png"
        self.assertEqual(step.read_bytes(), b"step")
        self.assertTrue(self.new.hotkeys_file.exists())
        # a second restore finds nothing new
        again = self._plan(current_settings=settings, current_hotkeys=json.loads(self.new.hotkeys_file.read_text()))
        self.assertFalse(again.has_changes)
        self.assertEqual(again.procedures_skipped, ["20261025-070000-aaaaaa"])

    def test_restore_on_same_pc_keeps_paths(self):
        plan = backup.plan_restore(self.contents, self.old, current_settings={}, home=self.root / "old" / "home")
        self.assertEqual(plan.remapped, [])
        self.assertEqual(plan.history_added, [])  # entry already in the history
        self.assertEqual(plan.procedures_skipped, ["20261025-070000-aaaaaa"])


class RemapTests(unittest.TestCase):
    def test_windows_paths(self):
        pairs = [(r"C:\Users\old", r"C:\Users\new")]
        self.assertEqual(backup.remap_path(r"c:\users\OLD\Pictures\x.png", pairs), r"C:\Users\new\Pictures\x.png")
        self.assertEqual(backup.remap_path(r"C:\Users\older\x.png", pairs), r"C:\Users\older\x.png")
        self.assertEqual(backup.remap_path(r"D:\Shots", pairs), r"D:\Shots")
        self.assertEqual(backup.remap_path("", pairs), "")


class SettingsReplaceTests(unittest.TestCase):
    def test_replace_notifies_changed_keys(self):
        from screenpilot.settings import Settings

        settings = Settings(None)
        heard = []
        settings.add_listener(lambda key, value: heard.append((key, value)))
        changed = settings.replace({"schema_version": 1, "general": {"theme": "dark"}, "recording": {"gif_fps": 12}})
        self.assertEqual(settings.get("general.theme"), "dark")
        self.assertEqual(settings.get("recording.gif_fps"), 15)  # migrated like a normal load
        self.assertEqual(changed, ["general.theme"])
        self.assertEqual(heard, [("general.theme", "dark")])


class AppBackupTests(unittest.TestCase):
    """The headless layer used by the tray, Settings, the CLI and DevPilot."""

    def setUp(self):
        from screenpilot.app import create_app
        from screenpilot.core.logging_setup import close_logging
        from fixtures import make_backend
        from test_workflow import FakeClipboard, FakeShell

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(close_logging)
        self.app = create_app(
            paths=AppPaths.under(Path(self._tmp.name) / "profile", portable=True),
            backend=make_backend(),
            clipboard=FakeClipboard(),
            shell=FakeShell(),
        )

    def test_backup_now_remembers_time_and_rotates(self):
        from screenpilot.app import backup as app_backup

        self.app.settings.set("backup.keep", 2)
        for minute in range(3):
            result = app_backup.backup_now(self.app, now=datetime(2026, 10, 25, 8, minute, 0))
        self.assertEqual(result.path.parent, Path(self._tmp.name) / "profile" / "Backups")
        self.assertEqual(len(backup.list_backups(result.path.parent)), 2)
        self.assertEqual(self.app.settings.get("backup.last"), "2026-10-25T08:02:00")
        self.assertFalse(app_backup.is_due(self.app, now=datetime(2026, 10, 25, 9, 0)))
        self.app.settings.set("backup.mode", "manual")
        self.assertFalse(app_backup.is_due(self.app, now=datetime(2026, 10, 26, 9, 0)))

    def test_restore_reloads_live_settings_hotkeys_and_history(self):
        from screenpilot.app import backup as app_backup
        from screenpilot.capture import Rect

        self.app.settings.set("general.theme", "dark")
        self.app.hotkeys.assign("capture.copy", "Ctrl+Alt+K")
        result = self.app.capture.capture_region(Rect(0, 0, 20, 10))
        self.app.run_after_capture(result)
        self.assertEqual(len(self.app.history.entries()), 1)
        made = app_backup.backup_now(self.app)

        self.app.settings.set("general.theme", "light")
        self.app.hotkeys.assign("capture.copy", None)
        self.app.history.clear()
        heard = []
        self.app.settings.add_listener(lambda key, value: heard.append(key))

        plan = app_backup.plan_restore(self.app, made.path)
        self.assertIn("general.theme", plan.settings_changes)
        self.assertNotIn("backup.last", plan.settings_changes)
        self.assertEqual(plan.hotkey_changes, ["capture.copy"])
        self.assertEqual(len(plan.history_added), 1)
        done = app_backup.restore(self.app, plan)

        self.assertTrue(done.safety.name.endswith("-before-restore.zip"))
        self.assertEqual(self.app.settings.get("general.theme"), "dark")
        self.assertIn("general.theme", heard)
        self.assertEqual(str(self.app.hotkeys.binding("capture.copy")), "Ctrl+Alt+K")
        self.assertEqual(len(self.app.history.entries()), 1)
        # the safety copy holds the state from just before the restore
        safety = backup.read_backup(done.safety)
        self.assertEqual(safety.settings["general"]["theme"], "light")
        self.assertEqual(backup.list_backups(made.path.parent), [made.path])


if __name__ == "__main__":
    unittest.main()
