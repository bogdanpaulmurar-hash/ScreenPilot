"""Single instance, forwarded actions and DevPilot integration (no Qt needed)."""

import contextlib
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from screenpilot.instance import KEY_FILE, InstanceServer, action_message, address, send
from screenpilot.platform import devpilot


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "d"
        self.received = []
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.stop()
        self._tmp.cleanup()

    def server(self, reply=None):
        def handler(message):
            self.received.append(message)
            return reply or {"ok": True}

        server = InstanceServer(self.data, handler)
        self.servers.append(server)
        return server

    def test_no_instance_means_none(self):
        self.assertIsNone(send(self.data, {"cmd": "ping"}))

    def test_actions_are_forwarded_to_the_running_instance(self):
        self.assertTrue(self.server().start())
        self.assertEqual(send(self.data, {"cmd": "ping"}), {"ok": True})
        reply = send(self.data, action_message("capture.delayed", seconds=5, path=None))
        self.assertEqual(reply, {"ok": True})
        self.assertEqual(self.received, [{"cmd": "action", "action": "capture.delayed", "args": {"seconds": 5}}])

    def test_second_instance_cannot_take_the_channel(self):
        self.assertTrue(self.server().start())
        self.assertFalse(self.server().start())
        self.assertEqual(send(self.data, {"cmd": "ping"}), {"ok": True})  # the first one still answers

    def test_wrong_key_is_rejected(self):
        self.assertTrue(self.server().start())
        (self.data / KEY_FILE).write_bytes(b"x" * 32)  # a client with another key
        self.assertNotEqual((send(self.data, {"cmd": "ping"}) or {}).get("ok"), True)

    def test_stale_socket_of_a_crashed_instance_is_replaced(self):
        if os.name == "nt":
            self.skipTest("named pipes disappear with their process")
        addr, _ = address(self.data)
        self.data.mkdir(parents=True)
        Path(addr).write_text("")  # left behind by a crash
        self.assertTrue(self.server().start())
        self.assertEqual(send(self.data, {"cmd": "ping"}), {"ok": True})

    def test_stop_releases_the_channel(self):
        first = self.server()
        self.assertTrue(first.start())
        first.stop()
        self.assertTrue(self.server().start())

    def test_handler_errors_are_reported(self):
        def boom(message):
            raise RuntimeError("nope")

        server = InstanceServer(self.data, boom)
        self.servers.append(server)
        server.start()
        reply = send(self.data, action_message("capture.region"))
        self.assertFalse(reply["ok"])
        self.assertIn("nope", reply["error"])

    def test_concurrent_requests(self):
        self.assertTrue(self.server().start())
        replies = []
        threads = [threading.Thread(target=lambda: replies.append(send(self.data, {"cmd": "ping"}))) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(replies, [{"ok": True}] * 5)


class DevPilotTests(unittest.TestCase):
    def test_suppression_only_when_managed(self):
        env = {"DEVPILOT_MANAGED": "1", "DEVPILOT_SUPPRESS": "tray, Autostart"}
        self.assertTrue(devpilot.owns("tray", env))
        self.assertTrue(devpilot.owns("autostart", env))
        self.assertFalse(devpilot.owns("keepawake", env))
        self.assertFalse(devpilot.owns("tray", {"DEVPILOT_SUPPRESS": "tray"}))  # not started by DevPilot
        self.assertFalse(devpilot.managed({}))

    def test_manifest_actions_are_known_to_the_app(self):
        import json

        from screenpilot.__main__ import remote_action_ids

        manifest = json.loads((Path(__file__).parents[1] / "devpilot.tool.json").read_text(encoding="utf-8"))
        ids = remote_action_ids()
        for action in manifest["actions"]:
            if action["kind"] == "Arguments":
                parts = action["arguments"].split()
                self.assertEqual(parts[0], "action")
                self.assertIn(parts[1], ids, action["label"])


class StartupCommandTests(unittest.TestCase):
    def test_gui_arguments(self):
        from screenpilot.ui.main import startup_command

        self.assertEqual(startup_command(["x"]), action_message("app.show_hub"))
        self.assertEqual(startup_command(["x", "--autostart"]), {"cmd": "ping"})
        self.assertEqual(
            startup_command(["x", "--action", "capture.delayed", "--seconds", "3"]),
            {"cmd": "action", "action": "capture.delayed", "args": {"seconds": 3.0}},
        )


class CliActionTests(unittest.TestCase):
    def run_cli(self, *argv):
        from screenpilot.__main__ import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_list_unknown_forward_and_not_running(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"SCREENPILOT_HOME": tmp}):
            code, out, _ = self.run_cli("action", "--list")
            self.assertEqual(code, 0)
            self.assertIn("capture.region", out)
            self.assertEqual(self.run_cli("action", "nope.nope")[0], 2)
            self.assertEqual(self.run_cli("action", "capture.region", "--no-start")[0], 1)

            from screenpilot.core.paths import resolve_paths

            received = []
            server = InstanceServer(resolve_paths().data_dir, lambda m: received.append(m) or {"ok": True})
            self.assertTrue(server.start())
            try:
                self.assertEqual(self.run_cli("action", "capture.edit")[0], 0)
                self.assertEqual(received[-1]["action"], "capture.edit")
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
