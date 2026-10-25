"""Capture history: the most recent saved captures, with thumbnails.

Stored locally as JSON in the data directory; nothing leaves the machine.
Entries whose file was deleted or moved are pruned on load.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from screenpilot.capture.image import CaptureResult
from screenpilot.core.events import Event, EventBus, Subscription

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
THUMB_SIZE = (240, 160)
EVENT_HISTORY_CHANGED = "history.changed"


@dataclass(frozen=True)
class HistoryEntry:
    id: str
    path: str
    mode: str
    width: int
    height: int
    timestamp: str  # ISO 8601, UTC
    title: str = ""
    thumbnail: str = ""

    @property
    def created(self) -> datetime:
        return datetime.fromisoformat(self.timestamp)


class CaptureHistory:
    def __init__(self, data_dir: Path, *, limit: int = DEFAULT_LIMIT, exists: Callable[[Path], bool] = Path.exists):
        self.file = data_dir / "history.json"
        self.thumb_dir = data_dir / "thumbnails"
        self.limit = limit
        self._exists = exists
        self._lock = threading.RLock()
        self._entries: list[HistoryEntry] = []
        self._bus: EventBus | None = None
        self._sub: Subscription | None = None

    # -- persistence ----------------------------------------------------------
    def load(self) -> "CaptureHistory":
        if not self.file.exists():
            return self
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
            entries = [HistoryEntry(**item) for item in raw.get("entries", [])]
        except (OSError, ValueError, TypeError) as exc:
            log.error("Capture history unreadable, starting empty: %s", exc)
            entries = []
        with self._lock:
            self._entries = [e for e in entries if self._exists(Path(e.path))][: self.limit]
        return self

    def reload(self) -> None:
        """Read the file again (after a restore) and tell the UI."""
        with self._lock:
            self._entries = []
        self.load()
        self._notify()

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = {"version": 1, "entries": [asdict(e) for e in self._entries]}
        fd, tmp = tempfile.mkstemp(prefix=".history-", suffix=".tmp", dir=self.file.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- operations -----------------------------------------------------------
    def entries(self) -> list[HistoryEntry]:
        with self._lock:
            return list(self._entries)

    def add(self, result: CaptureResult, path: Path) -> HistoryEntry:
        entry_id = uuid.uuid4().hex
        entry = HistoryEntry(
            id=entry_id,
            path=str(path),
            mode=result.mode,
            width=result.image.width,
            height=result.image.height,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            title=str(result.metadata.get("window_title", "")),
            thumbnail=self._make_thumbnail(result, entry_id),
        )
        with self._lock:
            self._entries.insert(0, entry)
            dropped = self._entries[self.limit :]
            self._entries = self._entries[: self.limit]
        for old in dropped:
            self._delete_thumbnail(old)
        self._save()
        self._notify()
        return entry

    def set_limit(self, limit: int) -> None:
        """Change how many captures are kept; extra (oldest) entries are dropped now."""
        self.limit = max(1, min(MAX_LIMIT, int(limit)))
        with self._lock:
            dropped = self._entries[self.limit :]
            self._entries = self._entries[: self.limit]
        if dropped:
            for old in dropped:
                self._delete_thumbnail(old)
            self._save()
            self._notify()

    def remove(self, entry_id: str) -> None:
        """Forget an entry (the capture file itself is kept)."""
        with self._lock:
            removed = [e for e in self._entries if e.id == entry_id]
            self._entries = [e for e in self._entries if e.id != entry_id]
        for entry in removed:
            self._delete_thumbnail(entry)
        if removed:
            self._save()
            self._notify()

    def clear(self) -> None:
        with self._lock:
            old, self._entries = self._entries, []
        for entry in old:
            self._delete_thumbnail(entry)
        self._save()
        self._notify()

    def search(self, query: str) -> list[HistoryEntry]:
        q = query.strip().lower()
        return [e for e in self.entries() if not q or q in Path(e.path).name.lower() or q in e.title.lower() or q in e.mode]

    # -- event wiring ---------------------------------------------------------
    def attach(self, bus: EventBus) -> None:
        """Record every capture that the workflow saved to disk."""
        self._bus = bus
        self._sub = bus.subscribe("workflow.finished", self._on_workflow_finished)

    def _on_workflow_finished(self, event: Event) -> None:
        path, result = event.payload.get("path"), event.payload.get("result")
        if path is not None and result is not None:
            self.add(result, Path(path))

    def _notify(self) -> None:
        if self._bus is not None:
            self._bus.publish(EVENT_HISTORY_CHANGED)

    # -- thumbnails -----------------------------------------------------------
    def _make_thumbnail(self, result: CaptureResult, entry_id: str) -> str:
        try:
            image = result.image.to_pil()
            image.thumbnail(THUMB_SIZE)
            self.thumb_dir.mkdir(parents=True, exist_ok=True)
            target = self.thumb_dir / f"{entry_id}.png"
            image.save(target, "PNG")
            return str(target)
        except Exception:  # noqa: BLE001 - a thumbnail must never break a capture
            log.exception("Could not create thumbnail")
            return ""

    @staticmethod
    def _delete_thumbnail(entry: HistoryEntry) -> None:
        if entry.thumbnail:
            Path(entry.thumbnail).unlink(missing_ok=True)
