"""Video project: a source video, a trim range and timed annotations.

Headless and serialisable (JSON) so the desktop editor, the CLI and
Autopilot build the same projects. Coordinates are **video pixels**; times
are **seconds on the source timeline** (trimming does not move annotations).

Annotations are editor :class:`~screenpilot.editor.document.Shape` values
plus a ``start`` / ``end`` time. Step numbers follow the order in which steps
appear in the video (start time, then creation order), so moving a step
earlier renumbers the others automatically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from screenpilot.capture.geometry import Point
from screenpilot.editor.document import COLORS, Shape

ANNOTATION_KINDS = ("step", "arrow", "rect", "highlight", "text", "blur")
MIN_ANNOTATION_SECONDS = 0.2
MIN_OUTPUT_SECONDS = 0.2
UNDO_LIMIT = 200
PROJECT_FORMAT = "screenpilot.video"
PROJECT_VERSION = 1
DEFAULT_WIDTH = 5
STEP_FONT = 22


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration: float
    fps: float = 30.0
    has_audio: bool = False
    codec: str = ""
    kind: str = "mp4"  # "mp4" (anything ffmpeg reads) or "gif"


@dataclass(frozen=True)
class TimedAnnotation:
    id: int
    shape: Shape
    start: float
    end: float

    @property
    def kind(self) -> str:
        return self.shape.kind

    def active(self, t: float) -> bool:
        return self.start <= t < self.end


@dataclass
class VideoProject:
    source: Path
    info: VideoInfo
    trim_start: float = 0.0
    trim_end: float | None = None  # None = end of the video
    annotations: list[TimedAnnotation] = field(default_factory=list)
    _next_id: int = 1
    _undo: list[tuple] = field(default_factory=list, repr=False)
    _redo: list[tuple] = field(default_factory=list, repr=False)

    # -- time -----------------------------------------------------------------------
    @property
    def duration(self) -> float:
        return self.info.duration

    @property
    def start(self) -> float:
        return self.trim_start

    @property
    def end(self) -> float:
        return self.duration if self.trim_end is None else self.trim_end

    @property
    def output_duration(self) -> float:
        return max(0.0, self.end - self.start)

    def clamp_time(self, t: float) -> float:
        return min(max(0.0, float(t)), self.duration)

    @property
    def edited(self) -> bool:
        return bool(self.annotations) or self.trim_start > 0 or self.trim_end is not None

    # -- history --------------------------------------------------------------------
    def _state(self) -> tuple:
        return (list(self.annotations), self.trim_start, self.trim_end)

    def _snapshot(self) -> None:
        self._undo.append(self._state())
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()

    def _restore(self, state: tuple) -> None:
        self.annotations, self.trim_start, self.trim_end = list(state[0]), state[1], state[2]

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._state())
        self._restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._state())
        self._restore(self._redo.pop())
        return True

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    # -- trim -----------------------------------------------------------------------
    def set_trim(self, start: float | None = None, end: float | None = None, *, record: bool = True) -> None:
        """Change one or both ends; the range always keeps at least ``MIN_OUTPUT_SECONDS``."""
        new_start = self.trim_start if start is None else self.clamp_time(start)
        new_end = self.end if end is None else self.clamp_time(end)
        if start is not None and new_start > new_end - MIN_OUTPUT_SECONDS:
            new_start = max(0.0, new_end - MIN_OUTPUT_SECONDS)
        if end is not None and new_end < new_start + MIN_OUTPUT_SECONDS:
            new_end = min(self.duration, new_start + MIN_OUTPUT_SECONDS)
        trim_end = None if new_end >= self.duration - 1e-6 else new_end
        if (new_start, trim_end) == (self.trim_start, self.trim_end):
            return
        if record:
            self._snapshot()
        self.trim_start, self.trim_end = new_start, trim_end

    def reset_trim(self) -> None:
        if self.trim_start or self.trim_end is not None:
            self._snapshot()
            self.trim_start, self.trim_end = 0.0, None

    # -- annotations ----------------------------------------------------------------
    def make_shape(
        self, kind: str, p1: Point, p2: Point | None = None, *, text: str = "", color: str = COLORS["red"]
    ) -> Shape:
        if kind not in ANNOTATION_KINDS:
            raise ValueError(f"Unknown annotation kind {kind}")
        w, h = self.info.width, self.info.height

        def clamp(p: Point) -> Point:
            return Point(min(max(int(p.x), 0), w - 1), min(max(int(p.y), 0), h - 1))

        a = clamp(p1)
        b = clamp(p2 or p1)
        return Shape(kind, a, b, color=color, width=DEFAULT_WIDTH, text=text, font_size=STEP_FONT, fill=kind == "text", strength=14)

    def _times(self, start: float, end: float) -> tuple[float, float]:
        start = self.clamp_time(start)
        end = self.clamp_time(end)
        if end - start < MIN_ANNOTATION_SECONDS:
            end = min(self.duration, start + MIN_ANNOTATION_SECONDS)
            start = max(0.0, min(start, end - MIN_ANNOTATION_SECONDS))
        return start, end

    def add(self, shape: Shape, start: float, end: float) -> TimedAnnotation:
        if shape.kind not in ANNOTATION_KINDS:
            raise ValueError(f"Unknown annotation kind {shape.kind}")
        self._snapshot()
        s, e = self._times(start, end)
        ann = TimedAnnotation(self._next_id, shape, s, e)
        self._next_id += 1
        self.annotations.append(ann)
        return ann

    def get(self, ann_id: int) -> TimedAnnotation | None:
        return next((a for a in self.annotations if a.id == ann_id), None)

    def update(self, ann_id: int, *, shape: Shape | None = None, start: float | None = None, end: float | None = None, record: bool = True) -> TimedAnnotation | None:
        for index, ann in enumerate(self.annotations):
            if ann.id == ann_id:
                s, e = self._times(ann.start if start is None else start, ann.end if end is None else end)
                new = replace(ann, shape=shape or ann.shape, start=s, end=e)
                if new == ann:
                    return ann
                if record:
                    self._snapshot()
                self.annotations[index] = new
                return new
        return None

    def begin_change(self) -> None:
        """Record one undo step before a series of ``update(..., record=False)`` (drags)."""
        self._snapshot()

    def end_change(self) -> None:
        """Drop the undo step of :meth:`begin_change` when the drag changed nothing."""
        if self._undo and self._undo[-1] == self._state():
            self._undo.pop()

    def remove(self, ann_id: int) -> bool:
        if self.get(ann_id) is None:
            return False
        self._snapshot()
        self.annotations = [a for a in self.annotations if a.id != ann_id]
        return True

    # -- queries --------------------------------------------------------------------
    def step_numbers(self) -> dict[int, int]:
        steps = sorted((a for a in self.annotations if a.kind == "step"), key=lambda a: (a.start, a.id))
        return {a.id: n for n, a in enumerate(steps, start=1)}

    def active_at(self, t: float) -> list[TimedAnnotation]:
        """Annotations visible at source time ``t``, blur first (it reads pixels), then in creation order."""
        active = [a for a in self.annotations if a.active(t)]
        return [a for a in active if a.kind == "blur"] + [a for a in active if a.kind != "blur"]

    def in_output(self) -> list[TimedAnnotation]:
        """Annotations that overlap the trimmed range, with times relative to the output."""
        out = []
        for a in self.annotations:
            s, e = max(a.start, self.start), min(a.end, self.end)
            if e - s > 1e-6:
                out.append(replace(a, start=s - self.start, end=e - self.start))
        return out

    # -- serialisation --------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "format": PROJECT_FORMAT,
            "version": PROJECT_VERSION,
            "source": str(self.source),
            "trim": [round(self.trim_start, 3), None if self.trim_end is None else round(self.trim_end, 3)],
            "annotations": [
                {
                    "kind": a.kind,
                    "p1": [a.shape.p1.x, a.shape.p1.y],
                    "p2": [a.shape.p2.x, a.shape.p2.y],
                    "start": round(a.start, 3),
                    "end": round(a.end, 3),
                    "color": a.shape.color,
                    **({"text": a.shape.text} if a.shape.text else {}),
                }
                for a in self.annotations
            ],
        }

    def apply_dict(self, data: dict[str, Any]) -> "VideoProject":
        """Load trim and annotations (the source and its info are already known)."""
        if data.get("format", PROJECT_FORMAT) != PROJECT_FORMAT:
            raise ValueError("Not a ScreenPilot video project")
        trim = data.get("trim") or [0, None]
        start = float(trim[0] or 0)
        end = None if len(trim) < 2 or trim[1] is None else float(trim[1])
        self.set_trim(start, end, record=False)
        for raw in data.get("annotations", []):
            kind = str(raw["kind"])
            p1 = Point(*[int(v) for v in raw["p1"]])
            p2 = Point(*[int(v) for v in raw.get("p2", raw["p1"])])
            shape = self.make_shape(kind, p1, p2, text=str(raw.get("text", "")), color=str(raw.get("color", COLORS["red"])))
            self.add(shape, float(raw["start"]), float(raw["end"]))
        self._undo.clear()
        return self

    def save(self, path: Path) -> Path:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path


def assign_lanes(annotations: list[TimedAnnotation]) -> dict[int, int]:
    """Timeline rows: annotations that overlap in time go to different rows (greedy, by start)."""
    lanes: list[float] = []  # end time of the last annotation in each row
    out: dict[int, int] = {}
    for ann in sorted(annotations, key=lambda a: (a.start, a.id)):
        for index, end in enumerate(lanes):
            if end <= ann.start + 1e-9:
                lanes[index] = ann.end
                out[ann.id] = index
                break
        else:
            lanes.append(ann.end)
            out[ann.id] = len(lanes) - 1
    return out


def format_time(seconds: float) -> str:
    """``01:02.5`` (or ``1:01:02.5`` past an hour) — tenths matter when cutting."""
    seconds = max(0.0, seconds)
    tenths = int(round(seconds * 10))
    total, tenth = divmod(tenths, 10)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}.{tenth}" if h else f"{m:02d}:{s:02d}.{tenth}"


def read_project_file(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "source" not in data:
        raise ValueError("Not a ScreenPilot video project")
    return data
