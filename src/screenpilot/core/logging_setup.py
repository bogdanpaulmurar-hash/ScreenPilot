"""Logging configuration.

Logs go to a size-capped rotating file in the user's log directory. Nothing is
ever sent over the network: ScreenPilot has no telemetry.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(name)s:%(lineno)d: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5


def set_level(level: int) -> None:
    logging.getLogger("screenpilot").setLevel(level)


def close_logging() -> None:
    """Flush and close ScreenPilot's log handlers (releases the file lock on Windows)."""
    root = logging.getLogger("screenpilot")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def configure_logging(log_dir: Path | None, *, level: int = logging.INFO, console: bool = False) -> None:
    root = logging.getLogger("screenpilot")
    root.setLevel(level)
    close_logging()

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "screenpilot.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        errors = logging.handlers.RotatingFileHandler(
            log_dir / "errors.log", maxBytes=MAX_BYTES, backupCount=2, encoding="utf-8"
        )
        errors.setLevel(logging.WARNING)
        errors.setFormatter(formatter)
        root.addHandler(errors)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    root.propagate = False
