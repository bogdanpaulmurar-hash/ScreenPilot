"""Shared test setup.

A per-test watchdog: when a test hangs (for example on a modal dialog that
nobody closes in CI), dump every thread's stack and exit, so the CI log shows
where it stopped instead of running until the job timeout.
``SCREENPILOT_TEST_TIMEOUT`` (seconds, default 180; 0 turns it off).
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading

import pytest

TIMEOUT = float(os.environ.get("SCREENPILOT_TEST_TIMEOUT", "180") or 0)


@pytest.fixture(autouse=True)
def _hang_watchdog(request):
    if TIMEOUT <= 0:
        yield
        return
    finished = threading.Event()
    capman = request.config.pluginmanager.getplugin("capturemanager")

    def watch() -> None:
        if finished.wait(TIMEOUT):
            return
        try:
            if capman is not None:
                capman.suspend_global_capture(in_=True)
        except Exception:  # noqa: BLE001 - best effort, we are about to exit
            pass
        sys.stderr.write(f"\n[watchdog] {request.node.nodeid} still running after {TIMEOUT:.0f} s\n")
        sys.stderr.flush()
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
        sys.stdout.flush()
        os._exit(3)

    threading.Thread(target=watch, name="test-watchdog", daemon=True).start()
    try:
        yield
    finally:
        finished.set()
