"""Single running instance + commands forwarded to it.

The desktop app listens on a per-user local channel; a second start (or
``python -m screenpilot action capture.region`` from DevPilot, a shortcut or a
script) hands its request to the running app and exits, instead of starting
a second tray icon that cannot register the hotkeys.

* Windows: a named pipe ``\\\\.\\pipe\\ScreenPilot-<hash>``; elsewhere a Unix socket in
  the data folder. Only the standard library (``multiprocessing.connection``).
* Every connection is authenticated with a random key stored in the user's
  data folder (HMAC challenge), so other users and processes without access
  to that folder cannot drive the app. Nothing goes over the network.
* Creating the listener is also the instance lock: Windows refuses a second
  "first instance" of the same pipe.

Messages are small dicts: ``{"cmd": "action", "action": "capture.region",
"args": {...}}`` or ``{"cmd": "ping"}``; replies are ``{"ok": bool, ...}``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sys
import threading
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

KEY_FILE = "instance.key"
SOCKET_FILE = "instance.sock"
REPLY_TIMEOUT = 5.0

Handler = Callable[[dict[str, Any]], dict[str, Any]]


def _key(data_dir: Path) -> bytes:
    path = Path(data_dir) / KEY_FILE
    try:
        key = path.read_bytes()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(key)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return key


def address(data_dir: Path) -> tuple[str, str]:
    """(address, family) of the instance channel for this data folder."""
    if sys.platform == "win32":
        digest = hashlib.sha1(str(Path(data_dir).resolve()).lower().encode("utf-8")).hexdigest()[:16]
        return rf"\\.\pipe\ScreenPilot-{digest}", "AF_PIPE"
    return str(Path(data_dir) / SOCKET_FILE), "AF_UNIX"


def send(data_dir: Path, message: dict[str, Any], *, timeout: float = REPLY_TIMEOUT) -> dict[str, Any] | None:
    """Send ``message`` to the running app; ``None`` when no instance is running."""
    addr, family = address(data_dir)
    if family == "AF_UNIX" and not Path(addr).exists():
        return None
    try:
        key = _key(data_dir)
        conn = Client(addr, family=family, authkey=key)
    except AuthenticationError:
        # Something answers but with another key: never start a second app next to it.
        log.warning("The running instance rejected the key in %s", Path(data_dir) / KEY_FILE)
        return {"ok": False, "error": "authentication failed"}
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, OSError) as exc:
        log.debug("No running instance at %s: %s", addr, exc)
        return None
    try:
        conn.send(message)
        if conn.poll(timeout):
            reply = conn.recv()
            return reply if isinstance(reply, dict) else {"ok": False, "error": "bad reply"}
        return {"ok": False, "error": "timeout"}
    except (EOFError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


class InstanceServer:
    """Listens for forwarded commands; ``handler`` runs on the listener thread."""

    def __init__(self, data_dir: Path, handler: Handler) -> None:
        self.data_dir = Path(data_dir)
        self.handler = handler
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    def start(self) -> bool:
        """Become the instance; ``False`` when another instance already owns the channel."""
        addr, family = address(self.data_dir)
        key = _key(self.data_dir)
        try:
            self._listener = Listener(addr, family=family, authkey=key)
        except OSError as exc:
            if family == "AF_UNIX" and send(self.data_dir, {"cmd": "ping"}) is None:
                Path(addr).unlink(missing_ok=True)  # stale socket of a crashed instance
                self._listener = Listener(addr, family=family, authkey=key)
            else:
                log.info("Another ScreenPilot instance owns %s (%s)", addr, exc)
                return False
        self._thread = threading.Thread(target=self._serve, name="instance-server", daemon=True)
        self._thread.start()
        log.info("Instance channel ready")
        return True

    def _serve(self) -> None:
        listener = self._listener
        while not self._stopping and listener is not None:
            try:
                conn = listener.accept()
            except Exception as exc:  # noqa: BLE001 - wrong key, client gone, listener closed
                if self._stopping:
                    return
                log.warning("Rejected an instance connection: %s", exc)
                continue
            threading.Thread(target=self._handle, args=(conn,), name="instance-request", daemon=True).start()

    def _handle(self, conn) -> None:
        try:
            if not conn.poll(REPLY_TIMEOUT):
                return
            message = conn.recv()
            if not isinstance(message, dict):
                conn.send({"ok": False, "error": "bad request"})
                return
            if message.get("cmd") == "ping":
                conn.send({"ok": True})
                return
            if message.get("cmd") == "_stop":
                conn.send({"ok": self._stopping})
                return
            try:
                reply = self.handler(message)
            except Exception as exc:  # noqa: BLE001
                log.exception("Forwarded command failed")
                reply = {"ok": False, "error": str(exc)}
            conn.send(reply)
        except (EOFError, OSError) as exc:
            log.debug("Instance request ended: %s", exc)
        finally:
            conn.close()

    def stop(self) -> None:
        if self._listener is None:
            return
        self._stopping = True
        send(self.data_dir, {"cmd": "_stop"}, timeout=1.0)  # wakes accept() so the thread ends
        try:
            self._listener.close()
        except OSError:
            pass
        self._listener = None


def action_message(action: str, **args: Any) -> dict[str, Any]:
    return {"cmd": "action", "action": action, "args": {k: v for k, v in args.items() if v is not None}}
