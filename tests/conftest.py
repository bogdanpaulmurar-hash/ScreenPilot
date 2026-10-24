"""Shared test setup.

A per-test watchdog: when a test hangs (for example on a modal dialog that
nobody closes in CI), dump every thread's stack and exit, so the CI log shows
where it stopped instead of running until the job timeout.
``SCREENPILOT_TEST_TIMEOUT`` (seconds, default 180; 0 turns it off).
"""

from __future__ import annotations

import faulthandler
import os
import threading

import pytest

TIMEOUT = float(os.environ.get("SCREENPILOT_TEST_TIMEOUT", "180") or 0)


@pytest.fixture(autouse=True)
def _hang_watchdog(request):
    if TIMEOUT <= 0:
        yield
        return
    finished = threading.Event()

    def watch() -> None:
        if finished.wait(TIMEOUT):
            return
        path = os.environ.get("SCREENPILOT_WATCHDOG_LOG", "watchdog.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"[watchdog] {request.node.nodeid} still running after {TIMEOUT:.0f} s\n")
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)
        os._exit(3)

    threading.Thread(target=watch, name="test-watchdog", daemon=True).start()
    try:
        yield
    finally:
        finished.set()
