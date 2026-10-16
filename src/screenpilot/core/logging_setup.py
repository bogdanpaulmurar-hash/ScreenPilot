"""Logging configuration.

Logs go to a size-capped rotating file in the user's log directory. Nothing is
ever sent over the network: ScreenPilot has no telemetry.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5


def configure_logging(log_dir: Path | None, *, level: int = logging.INFO, console: bool = False) -> None:
    root = logging.getLogger("screenpilot")
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "screenpilot.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    root.propagate = False
