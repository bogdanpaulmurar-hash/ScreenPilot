"""Diagnostics: timing journal, crash capture and a shareable support bundle.

* ``timed("capture.freeze")`` records how long an operation took. Timings go
  to ``perf.jsonl`` (one JSON object per line) and to an in-memory ring
  buffer shown in Settings › Diagnostics with min / median / p95 / max.
* Unhandled exceptions (any thread), Qt warnings and native crashes
  (``faulthandler``) are written to ``crash.log`` with full context.
* :func:`export_bundle` zips logs, timings, settings (secrets-free) and system
  info into one file the user can send — no network involved.

Privacy: user file paths are kept (needed to debug saving) but the Windows
user name is replaced with ``<user>`` in exported text files.
"""

from __future__ import annotations

import faulthandler
import getpass
import json
import logging
import os
import platform
import statistics
import sys
import threading
import time
import traceback
import zipfile
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from screenpilot import __version__

log = logging.getLogger(__name__)

RING_SIZE = 500
PERF_FILE = "perf.jsonl"
CRASH_FILE = "crash.log"
FAULT_FILE = "native-crash.log"
PERF_MAX_BYTES = 2_000_000
SLOW_MS = {  # operations slower than this are logged as warnings
    "capture.freeze": 250,
    "capture.grab": 250,
    "selector.open": 400,
    "detect.build": 500,
    "workflow.run": 1500,
}


@dataclass(frozen=True)
class Timing:
    name: str
    ms: float
    at: str
    extra: dict[str, Any]


class Diagnostics:
    def __init__(self, log_dir: Path | None) -> None:
        self.log_dir = log_dir
        self._lock = threading.Lock()
        self._ring: deque[Timing] = deque(maxlen=RING_SIZE)
        self._fault_file = None

    # -- timings --------------------------------------------------------------
    def record(self, name: str, ms: float, **extra: Any) -> Timing:
        timing = Timing(name, round(ms, 1), datetime.now(timezone.utc).isoformat(timespec="milliseconds"), extra)
        with self._lock:
            self._ring.append(timing)
            self._append_perf(timing)
        limit = SLOW_MS.get(name)
        if limit is not None and ms > limit:
            log.warning("Slow %s: %.0f ms (limit %d ms) %s", name, ms, limit, extra or "")
        else:
            log.debug("%s took %.1f ms %s", name, ms, extra or "")
        return timing

    @contextmanager
    def timed(self, name: str, **extra: Any) -> Iterator[dict[str, Any]]:
        """Context manager; the yielded dict can be filled with extra fields."""
        start = time.perf_counter()
        info: dict[str, Any] = dict(extra)
        try:
            yield info
        except Exception as exc:
            info["error"] = type(exc).__name__
            raise
        finally:
            self.record(name, (time.perf_counter() - start) * 1000, **info)

    def _append_perf(self, timing: Timing) -> None:
        if self.log_dir is None:
            return
        path = self.log_dir / PERF_FILE
        try:
            if path.exists() and path.stat().st_size > PERF_MAX_BYTES:
                path.replace(path.with_suffix(".1.jsonl"))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"name": timing.name, "ms": timing.ms, "at": timing.at, **timing.extra}) + "\n")
        except OSError:
            pass  # diagnostics must never break the app

    def timings(self) -> list[Timing]:
        with self._lock:
            return list(self._ring)

    def summary(self) -> dict[str, dict[str, float]]:
        """name -> count, last, min, median, p95, max (milliseconds)."""
        groups: dict[str, list[float]] = defaultdict(list)
        last: dict[str, float] = {}
        for t in self.timings():
            groups[t.name].append(t.ms)
            last[t.name] = t.ms
        result = {}
        for name, values in sorted(groups.items()):
            ordered = sorted(values)
            p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
            result[name] = {
                "count": len(values),
                "last": last[name],
                "min": ordered[0],
                "median": round(statistics.median(ordered), 1),
                "p95": p95,
                "max": ordered[-1],
            }
        return result

    # -- crash capture --------------------------------------------------------
    def install_crash_handlers(self) -> None:
        sys.excepthook = self._excepthook
        threading.excepthook = lambda args: self._excepthook(args.exc_type, args.exc_value, args.exc_traceback, args.thread)
        if self.log_dir is not None:
            try:
                self._fault_file = (self.log_dir / FAULT_FILE).open("a", encoding="utf-8")
                faulthandler.enable(self._fault_file, all_threads=True)
            except (OSError, RuntimeError):
                log.exception("faulthandler unavailable")

    def _excepthook(self, exc_type, exc, tb, thread=None) -> None:
        if exc_type is KeyboardInterrupt:
            return
        where = thread.name if thread is not None else threading.current_thread().name
        log.critical("Unhandled exception in %s", where, exc_info=(exc_type, exc, tb))
        self.write_crash(f"Unhandled exception in thread {where}", "".join(traceback.format_exception(exc_type, exc, tb)))

    def write_crash(self, title: str, details: str) -> None:
        if self.log_dir is None:
            return
        try:
            with (self.log_dir / CRASH_FILE).open("a", encoding="utf-8") as fh:
                fh.write(f"===== {datetime.now().isoformat(timespec='seconds')} · ScreenPilot {__version__} =====\n")
                fh.write(f"{title}\n{details}\n")
                recent = self.timings()[-10:]
                if recent:
                    fh.write("Recent operations: " + ", ".join(f"{t.name}={t.ms}ms" for t in recent) + "\n")
                fh.write("\n")
        except OSError:
            pass

    def close(self) -> None:
        if self._fault_file is not None:
            try:
                faulthandler.disable()
                self._fault_file.close()
            except (OSError, ValueError):
                pass
            self._fault_file = None


def system_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "screenpilot": __version__,
        "python": sys.version.split()[0],
        "executable": "frozen" if getattr(sys, "frozen", False) else "python",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "locale": os.environ.get("LANG", ""),
    }
    try:
        from PySide6 import __version__ as pyside

        info["pyside6"] = pyside
    except ImportError:
        info["pyside6"] = None
    try:
        import PIL

        info["pillow"] = PIL.__version__
    except ImportError:
        pass
    return info


def _anonymize(text: str) -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        return text
    if user and len(user) > 1:
        text = text.replace(f"\\{user}\\", "\\<user>\\").replace(f"/{user}/", "/<user>/")
    return text


def export_bundle(
    target: Path,
    *,
    log_dir: Path,
    settings: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    diagnostics: Diagnostics | None = None,
) -> Path:
    """Write a zip with everything needed to investigate a problem."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if log_dir.is_dir():
            for path in sorted(log_dir.iterdir()):
                is_log = path.suffix in (".log", ".jsonl") or ".log." in path.name or ".jsonl." in path.name
                if path.is_file() and is_log:
                    try:
                        zf.writestr(f"logs/{path.name}", _anonymize(path.read_text(encoding="utf-8", errors="replace")))
                    except OSError:
                        continue
        info = {"system": system_info(), "exported_at": datetime.now().isoformat(timespec="seconds")}
        if diagnostics is not None:
            info["timings"] = diagnostics.summary()
        if extra:
            info.update(extra)
        zf.writestr("system.json", _anonymize(json.dumps(info, indent=2, ensure_ascii=False, default=str)))
        if settings is not None:
            zf.writestr("settings.json", _anonymize(json.dumps(settings, indent=2, ensure_ascii=False)))
        zf.writestr(
            "README.txt",
            "ScreenPilot diagnostic bundle.\n"
            "Contains log files, operation timings, settings and system information.\n"
            "It contains no screenshots. The Windows user name is replaced with <user>.\n",
        )
    log.info("Diagnostic bundle written to %s", target)
    return target


# Process-wide instance, replaced by create_app(); safe no-op default for libraries/tests.
_current = Diagnostics(None)


def current() -> Diagnostics:
    return _current


def set_current(diag: Diagnostics) -> None:
    global _current
    _current = diag


def timed(name: str, **extra: Any):
    return _current.timed(name, **extra)
