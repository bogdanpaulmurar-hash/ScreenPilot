"""OCR Engine: text recognition behind a small, swappable interface.

Backends:
* :class:`WindowsOcrEngine` — the OCR engine built into Windows 10/11
  (Windows.Media.Ocr). No download, no dependency; reached through Windows
  PowerShell 5.1, which ships with Windows. Languages = the Windows language
  packs installed (Settings › Time & language › Language; add Romanian /
  German with the "Optical character recognition" feature).
* :class:`TesseractEngine` — optional; used when ``tesseract.exe`` is present
  (Apache-2.0). Gives per-word confidence.

Every backend returns an :class:`OcrResult` with words, lines and their
pixel rectangles, so the UI can highlight, search and blur.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from screenpilot.capture.geometry import Rect
from screenpilot.capture.image import CapturedImage
from screenpilot.core import diagnostics
from screenpilot.core.errors import ScreenPilotError

log = logging.getLogger(__name__)

TIMEOUT_S = 60
UPSCALE_BELOW = 900  # small captures are enlarged 2x: much better accuracy on UI text
LANGUAGE_TAGS = {"en": "en-US", "ro": "ro-RO", "de": "de-DE"}


class OcrError(ScreenPilotError):
    message_key = "error.ocr"


@dataclass(frozen=True)
class OcrWord:
    text: str
    rect: Rect
    confidence: float | None = None  # 0..100, None when the engine doesn't report it


@dataclass(frozen=True)
class OcrLine:
    words: tuple[OcrWord, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def rect(self) -> Rect:
        r = Rect(0, 0, 0, 0)
        for w in self.words:
            r = r.union(w.rect)
        return r


@dataclass(frozen=True)
class OcrResult:
    lines: tuple[OcrLine, ...]
    language: str = ""
    engine: str = ""
    angle: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def words(self) -> list[OcrWord]:
        return [w for line in self.lines for w in line.words]

    @property
    def confidence(self) -> float | None:
        values = [w.confidence for w in self.words if w.confidence is not None]
        return round(sum(values) / len(values), 1) if values else None

    def find(self, query: str) -> list[OcrWord]:
        q = query.strip().lower()
        return [w for w in self.words if q and q in w.text.lower()]

    def scaled(self, factor: float) -> "OcrResult":
        def s(r: Rect) -> Rect:
            return Rect(round(r.x / factor), round(r.y / factor), max(1, round(r.width / factor)), max(1, round(r.height / factor)))

        lines = tuple(OcrLine(tuple(OcrWord(w.text, s(w.rect), w.confidence) for w in l.words)) for l in self.lines)
        return OcrResult(lines, self.language, self.engine, self.angle, self.extra)


class OcrEngine(Protocol):
    name: str

    def available(self) -> bool: ...

    def languages(self) -> list[str]: ...

    def recognize(self, image: CapturedImage, language: str | None = None) -> OcrResult: ...


def _prepare(image: CapturedImage) -> tuple[CapturedImage, float]:
    if image.height < UPSCALE_BELOW and image.width < 4000:
        from PIL import Image

        pil = image.to_pil()
        pil = pil.resize((pil.width * 2, pil.height * 2), Image.Resampling.LANCZOS)
        return CapturedImage.from_pil(pil), 2.0
    return image, 1.0


_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result
}
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$mode = $args[0]
if ($mode -eq 'languages') {
    $langs = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag })
    @{ languages = $langs; max = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension } | ConvertTo-Json -Compress
    exit 0
}
$path = $args[1]; $tag = $args[2]
if ($tag) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($tag)) }
else { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { Write-Output '{"error":"language"}'; exit 3 }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$lines = @($result.Lines | ForEach-Object {
    ,@($_.Words | ForEach-Object { @{ t = $_.Text; x = [int]$_.BoundingRect.X; y = [int]$_.BoundingRect.Y;
        w = [int]$_.BoundingRect.Width; h = [int]$_.BoundingRect.Height } })
})
$stream.Dispose()
@{ lang = $engine.RecognizerLanguage.LanguageTag; angle = $result.TextAngle; lines = $lines } | ConvertTo-Json -Depth 6 -Compress
"""


class WindowsOcrEngine:
    """Windows.Media.Ocr through Windows PowerShell 5.1 (built into Windows)."""

    name = "windows"

    def __init__(self) -> None:
        self._languages: list[str] | None = None
        self._script: Path | None = None

    @staticmethod
    def _powershell() -> str | None:
        if sys.platform != "win32":
            return None
        root = os.environ.get("SystemRoot", r"C:\Windows")
        exe = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        return str(exe) if exe.exists() else shutil.which("powershell.exe")

    def _script_path(self) -> Path:
        if self._script is None or not self._script.exists():
            fd, name = tempfile.mkstemp(prefix="screenpilot-ocr-", suffix=".ps1")
            with os.fdopen(fd, "w", encoding="utf-8-sig") as fh:
                fh.write(_PS_SCRIPT)
            self._script = Path(name)
        return self._script

    def _run(self, *args: str) -> dict:
        exe = self._powershell()
        if exe is None:
            raise OcrError("Windows OCR is only available on Windows")
        cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self._script_path()), *args]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
                cmd, capture_output=True, timeout=TIMEOUT_S, creationflags=flags, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OcrError(f"Windows OCR could not run: {exc}") from exc
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        if not out:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            log.error("Windows OCR failed (exit %s): %s", proc.returncode, err[-800:])
            raise OcrError(f"Windows OCR failed: {err[-300:] or 'no output'}")
        try:
            data = json.loads(out.splitlines()[-1])
        except ValueError as exc:
            raise OcrError(f"Unexpected OCR output: {out[-200:]}") from exc
        if data.get("error") == "language":
            raise OcrError("The OCR language is not installed in Windows", message_key="error.ocr_language")
        return data

    def available(self) -> bool:
        try:
            return bool(self.languages())
        except OcrError:
            return False

    def languages(self) -> list[str]:
        if self._languages is None:
            data = self._run("languages")
            langs = data.get("languages") or []
            self._languages = [langs] if isinstance(langs, str) else list(langs)
        return self._languages

    def recognize(self, image: CapturedImage, language: str | None = None) -> OcrResult:
        prepared, factor = _prepare(image)
        tag = LANGUAGE_TAGS.get(language or "", language or "")
        fd, name = tempfile.mkstemp(prefix="screenpilot-ocr-", suffix=".png")
        os.close(fd)
        path = Path(name)
        try:
            prepared.save(path)
            with diagnostics.timed("ocr.recognize", engine=self.name, width=image.width, height=image.height):
                data = self._run("recognize", str(path), tag)
        finally:
            path.unlink(missing_ok=True)
        lines = []
        for raw_line in data.get("lines") or []:
            if isinstance(raw_line, dict):  # PowerShell collapses one-word lines
                raw_line = [raw_line]
            words = tuple(
                OcrWord(str(w.get("t", "")), Rect(int(w["x"]), int(w["y"]), max(1, int(w["w"])), max(1, int(w["h"]))))
                for w in raw_line
                if isinstance(w, dict) and w.get("t")
            )
            if words:
                lines.append(OcrLine(words))
        result = OcrResult(tuple(lines), data.get("lang", tag), self.name, data.get("angle"))
        return result.scaled(factor) if factor != 1.0 else result


class TesseractEngine:
    """Optional Tesseract backend (when tesseract.exe is installed or bundled)."""

    name = "tesseract"
    CODES = {"en": "eng", "ro": "ron", "de": "deu", "en-US": "eng", "ro-RO": "ron", "de-DE": "deu"}

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("tesseract") or self._default_path()

    @staticmethod
    def _default_path() -> str | None:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
            candidate = Path(base) / "Tesseract-OCR" / "tesseract.exe"
            if base and candidate.exists():
                return str(candidate)
        return None

    def available(self) -> bool:
        return bool(self.executable) and Path(self.executable).exists() if self.executable else False

    def languages(self) -> list[str]:
        if not self.available():
            return []
        proc = subprocess.run([self.executable, "--list-langs"], capture_output=True, timeout=20, check=False)  # noqa: S603
        names = proc.stdout.decode("utf-8", errors="replace").splitlines()[1:]
        reverse = {"eng": "en-US", "ron": "ro-RO", "deu": "de-DE"}
        return [reverse.get(n.strip(), n.strip()) for n in names if n.strip()]

    def recognize(self, image: CapturedImage, language: str | None = None) -> OcrResult:
        if not self.available():
            raise OcrError("Tesseract is not installed")
        prepared, factor = _prepare(image)
        code = self.CODES.get(language or "en", language or "eng")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.png"
            prepared.save(path)
            with diagnostics.timed("ocr.recognize", engine=self.name, width=image.width, height=image.height):
                proc = subprocess.run(  # noqa: S603
                    [self.executable, str(path), "stdout", "-l", code, "tsv"],
                    capture_output=True,
                    timeout=TIMEOUT_S,
                    check=False,
                )
        if proc.returncode != 0:
            raise OcrError(f"Tesseract failed: {proc.stderr.decode('utf-8', 'replace')[-300:]}")
        result = parse_tesseract_tsv(proc.stdout.decode("utf-8", errors="replace"), code)
        return result.scaled(factor) if factor != 1.0 else result


def parse_tesseract_tsv(tsv: str, language: str = "") -> OcrResult:
    lines: dict[tuple[int, int, int], list[OcrWord]] = {}
    rows = tsv.splitlines()
    for row in rows[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        key = (int(cols[2]), int(cols[3]), int(cols[4]))
        conf = float(cols[10])
        lines.setdefault(key, []).append(
            OcrWord(cols[11], Rect(int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9])), conf if conf >= 0 else None)
        )
    return OcrResult(tuple(OcrLine(tuple(ws)) for _, ws in sorted(lines.items())), language, "tesseract")


def default_engine(preferred: str = "auto") -> OcrEngine:
    """Best available engine: Windows OCR first (no dependency), Tesseract as fallback."""
    candidates: Sequence[OcrEngine]
    if preferred == "tesseract":
        candidates = (TesseractEngine(), WindowsOcrEngine())
    else:
        candidates = (WindowsOcrEngine(), TesseractEngine())
    for engine in candidates:
        if engine.available():
            return engine
    return candidates[0]
