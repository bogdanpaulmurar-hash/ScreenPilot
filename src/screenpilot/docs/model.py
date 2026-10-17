"""Procedure model and recording session (Documentation Engine).

A procedure is an ordered list of steps; each step has an image file, a
title and a description. Sessions are persisted as a folder with
``procedure.json`` + PNG files, so an interrupted session can be resumed
and big captures never sit in memory.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from screenpilot.capture.image import CapturedImage

log = logging.getLogger(__name__)

FILE = "procedure.json"


@dataclass
class ProcedureStep:
    image: str  # file name inside the session folder
    title: str = ""
    description: str = ""
    width: int = 0
    height: int = 0
    source: str = ""  # window title / mode, for the default text


@dataclass
class Procedure:
    title: str = ""
    author: str = ""
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="minutes"))
    introduction: str = ""
    steps: list[ProcedureStep] = field(default_factory=list)


class ProcedureSession:
    """A procedure being recorded, stored in its own folder."""

    def __init__(self, folder: Path, procedure: Procedure | None = None) -> None:
        self.folder = folder
        self.procedure = procedure or Procedure()

    # -- persistence ----------------------------------------------------------
    @classmethod
    def create(cls, root: Path, title: str = "", author: str = "") -> "ProcedureSession":
        folder = root / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
        folder.mkdir(parents=True, exist_ok=True)
        session = cls(folder, Procedure(title=title, author=author))
        session.save()
        return session

    @classmethod
    def load(cls, folder: Path) -> "ProcedureSession":
        data = json.loads((folder / FILE).read_text(encoding="utf-8"))
        steps = [ProcedureStep(**s) for s in data.pop("steps", [])]
        return cls(folder, Procedure(**data, steps=steps))

    def save(self) -> None:
        payload = asdict(self.procedure)
        fd, tmp = tempfile.mkstemp(prefix=".procedure-", suffix=".tmp", dir=self.folder)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.folder / FILE)

    def discard(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)

    @staticmethod
    def unfinished(root: Path) -> list[Path]:
        if not root.is_dir():
            return []
        return sorted((p for p in root.iterdir() if (p / FILE).is_file()), reverse=True)

    # -- steps ----------------------------------------------------------------
    @property
    def steps(self) -> list[ProcedureStep]:
        return self.procedure.steps

    def image_path(self, step: ProcedureStep) -> Path:
        return self.folder / step.image

    def add(self, image: CapturedImage, *, title: str = "", description: str = "", source: str = "") -> ProcedureStep:
        name = f"step-{uuid.uuid4().hex[:8]}.png"
        image.save(self.folder / name)
        step = ProcedureStep(name, title, description, image.width, image.height, source)
        self.steps.append(step)
        self.save()
        return step

    def replace_image(self, index: int, image: CapturedImage) -> None:
        step = self.steps[index]
        old = self.folder / step.image
        name = f"step-{uuid.uuid4().hex[:8]}.png"
        image.save(self.folder / name)
        step.image, step.width, step.height = name, image.width, image.height
        old.unlink(missing_ok=True)
        self.save()

    def remove(self, index: int) -> None:
        step = self.steps.pop(index)
        (self.folder / step.image).unlink(missing_ok=True)
        self.save()

    def move(self, index: int, new_index: int) -> None:
        step = self.steps.pop(index)
        self.steps.insert(max(0, min(len(self.steps), new_index)), step)
        self.save()

    def update(self, index: int, *, title: str | None = None, description: str | None = None) -> None:
        step = self.steps[index]
        if title is not None:
            step.title = title
        if description is not None:
            step.description = description
        self.save()
