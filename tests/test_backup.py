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


if __name__ == "__main__":
    unittest.main()
