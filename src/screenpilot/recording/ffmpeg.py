"""FFmpeg integration (optional, external process — never bundled by default).

ScreenPilot captures frames itself (GDI + effects) and pipes raw BGRA frames
to ``ffmpeg`` over stdin, which encodes them to H.264/MP4. Audio comes from
DirectShow devices opened by the same ffmpeg process. Using a separate
process means no FFmpeg code is linked into ScreenPilot, so the licence of
whichever FFmpeg build IT approves (LGPL or GPL) does not affect the app.

FFmpeg is located, in order: the ``recording.ffmpeg_path`` setting, an
``ffmpeg.exe`` next to the application (or in ``ffmpeg\\bin``), then ``PATH``.
Without it, recordings fall back to the built-in GIF encoder.

Hardware encoders (NVENC, Quick Sync, AMF, Media Foundation) are preferred in
``auto`` mode, but each one is verified with a tiny test encode first because
ffmpeg lists encoders the machine's GPU cannot actually use.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from screenpilot.core.errors import RecordingError

log = logging.getLogger(__name__)

HARDWARE_ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf", "h264_mf")
SOFTWARE_ENCODERS = ("libx264", "libopenh264", "mpeg4")
PROBE_TIMEOUT = 15
FINISH_TIMEOUT = 60

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _run(args: Sequence[str], timeout: float = PROBE_TIMEOUT) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_creation_flags(),
        stdin=subprocess.DEVNULL,
    )


# -- discovery ------------------------------------------------------------------
def candidate_paths(configured: str, app_dir: Path | None) -> list[Path]:
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    paths: list[Path] = []
    if configured:
        p = Path(os.path.expandvars(configured)).expanduser()
        paths.append(p / exe if p.is_dir() else p)
    if app_dir is not None:
        paths += [app_dir / exe, app_dir / "ffmpeg" / "bin" / exe, app_dir / "ffmpeg" / exe]
    found = shutil.which("ffmpeg")
    if found:
        paths.append(Path(found))
    return paths


def find_ffmpeg(configured: str = "", app_dir: Path | None = None) -> Path | None:
    for path in candidate_paths(configured, app_dir):
        if path.is_file():
            return path
    return None


def parse_version(text: str) -> str:
    match = re.search(r"ffmpeg version (\S+)", text)
    return match.group(1) if match else "unknown"


def parse_encoders(text: str) -> set[str]:
    """Names from ``ffmpeg -encoders``; video encoder lines start with ``V``."""
    names: set[str] = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):  # the legend ends with a "------" separator
        if line.strip().startswith("---"):
            lines = lines[i + 1 :]
            break
    for line in lines:
        match = re.match(r"\s*V[\w.]{5}\s+(\S+)", line)
        if match and match.group(1) != "=":
            names.add(match.group(1))
    return names


def parse_dshow_devices(text: str) -> list[tuple[str, str]]:
    """``(name, kind)`` pairs from ``ffmpeg -list_devices true -f dshow -i dummy``.

    Handles both the classic layout (section headers) and the newer one with
    ``(audio)`` / ``(video)`` after each name. Alternative-name lines are skipped.
    """
    devices: list[tuple[str, str]] = []
    section = ""
    for line in text.splitlines():
        if "DirectShow video devices" in line:
            section = "video"
            continue
        if "DirectShow audio devices" in line:
            section = "audio"
            continue
        if "Alternative name" in line:
            continue
        match = re.search(r'"([^"]+)"\s*(?:\((audio|video|none)\))?', line)
        if not match:
            continue
        kind = match.group(2) or section
        if kind in ("audio", "video") and (match.group(1), kind) not in devices:
            devices.append((match.group(1), kind))
    return devices


@dataclass
class FfmpegInfo:
    path: Path
    version: str
    encoders: set[str] = field(default_factory=set)
    _verified: dict[str, bool] = field(default_factory=dict)

    def works(self, encoder: str, runner: Runner = _run) -> bool:
        """Run a tiny test encode; results are cached per process."""
        if encoder not in self._verified:
            # Same encoder options as a real recording: e.g. h264_mf only fails
            # with -hw_encoding 1 on machines without a hardware encoder.
            args = [
                str(self.path), "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=gray:s=320x240:r=10:d=0.5",
                *encoder_args(encoder, 23, 10, 320, 240), "-f", "null", "-",
            ]
            try:
                ok = runner(args, timeout=PROBE_TIMEOUT).returncode == 0
            except (OSError, subprocess.SubprocessError) as exc:
                log.info("Encoder probe %s failed: %s", encoder, exc)
                ok = False
            log.info("Encoder %s %s", encoder, "works" if ok else "is not usable")
            self._verified[encoder] = ok
        return self._verified[encoder]

    def mark_broken(self, encoder: str) -> None:
        """Remember that an encoder failed during a real recording."""
        log.warning("Encoder %s failed during recording; it will not be chosen again this session", encoder)
        self._verified[encoder] = False

    def choose_encoder(self, mode: str = "auto", runner: Runner = _run) -> str:
        hardware = [e for e in HARDWARE_ENCODERS if e in self.encoders]
        software = [e for e in SOFTWARE_ENCODERS if e in self.encoders]
        order = {"hardware": hardware + software, "software": software + hardware}.get(mode, hardware + software)
        for encoder in order:
            if self.works(encoder, runner):
                return encoder
        raise RecordingError("No working video encoder found in ffmpeg", message_key="recording.error.no_encoder")

    def audio_devices(self, runner: Runner = _run) -> list[str]:
        if sys.platform != "win32":
            return []
        try:
            proc = runner(
                [str(self.path), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                timeout=PROBE_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("Listing audio devices failed: %s", exc)
            return []
        return [name for name, kind in parse_dshow_devices(proc.stderr) if kind == "audio"]


def probe(path: Path, runner: Runner = _run) -> FfmpegInfo:
    try:
        version = runner([str(path), "-hide_banner", "-version"]).stdout
        encoders = runner([str(path), "-hide_banner", "-encoders"]).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecordingError(f"ffmpeg at {path} cannot be started: {exc}", message_key="recording.error.ffmpeg_broken") from exc
    info = FfmpegInfo(path=path, version=parse_version(version), encoders=parse_encoders(encoders))
    log.info("ffmpeg %s at %s (%d video encoders)", info.version, path, len(info.encoders))
    return info


# -- commands -------------------------------------------------------------------
def even(value: int) -> int:
    """H.264 with 4:2:0 chroma needs even dimensions."""
    return max(2, value - (value % 2))


def encoder_args(encoder: str, quality: int, fps: int, width: int, height: int) -> list[str]:
    bitrate = max(500_000, int(width * height * fps * 0.12 * (23 / max(quality, 1))))
    common = ["-pix_fmt", "yuv420p", "-g", str(fps * 2)]
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(quality), *common]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(quality), "-b:v", "0", *common]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", str(quality), "-pix_fmt", "nv12", "-g", str(fps * 2)]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality + 2), *common]
    if encoder == "h264_mf":
        return ["-c:v", "h264_mf", "-hw_encoding", "1", "-b:v", str(bitrate), *common]
    if encoder == "libopenh264":
        return ["-c:v", "libopenh264", "-b:v", str(bitrate), *common]
    return ["-c:v", encoder, "-q:v", str(max(2, min(31, quality // 5))), *common]


def record_command(
    ffmpeg: Path,
    *,
    width: int,
    height: int,
    fps: int,
    output: Path,
    encoder: str,
    quality: int,
    microphone: str = "",
    system_audio: str = "",
    audio_offset_ms: int = 0,
) -> list[str]:
    """Audio inputs come first so they are open before the first video frame is read."""
    args = [str(ffmpeg), "-hide_banner", "-loglevel", "warning", "-y"]
    audio = [d for d in (microphone, system_audio) if d]
    for device in audio:
        if audio_offset_ms:
            args += ["-itsoffset", f"{audio_offset_ms / 1000:.3f}"]
        args += ["-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={device}"]
    args += [
        "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{width}x{height}",
        "-framerate", str(fps), "-i", "-",
    ]
    video_index = len(audio)
    args += ["-map", f"{video_index}:v"]
    if len(audio) == 2:
        args += ["-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[a]", "-map", "[a]"]
    elif len(audio) == 1:
        args += ["-map", "0:a"]
    args += encoder_args(encoder, quality, fps, width, height)
    if audio:
        args += ["-c:a", "aac", "-b:a", "160k", "-shortest"]
    args += ["-movflags", "+faststart", str(output)]
    return args


def concat_command(ffmpeg: Path, list_file: Path, output: Path) -> list[str]:
    return [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", str(output),
    ]


def gif_command(ffmpeg: Path, source: Path, output: Path, *, fps: int, max_width: int) -> list[str]:
    vf = (
        f"fps={fps},scale='min({max_width},iw)':-1:flags=lanczos,split[s0][s1];"
        "[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    )
    return [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vf", vf, "-loop", "0", str(output)]


def convert_to_gif(info: FfmpegInfo, source: Path, output: Path, *, fps: int = 12, max_width: int = 960) -> Path:
    proc = _run(gif_command(info.path, source, output, fps=fps, max_width=max_width), timeout=600)
    if proc.returncode != 0 or not output.exists():
        raise RecordingError(f"GIF conversion failed: {proc.stderr.strip()[-400:]}", message_key="recording.error.gif")
    return output


# -- sink -----------------------------------------------------------------------
class FfmpegSink:
    """Constant-frame-rate MP4 writer fed with BGRA frames.

    * ``write(frame, ts)`` maps the recording timestamp to frame slots; missed
      slots repeat the previous frame so video stays in sync with audio and
      wall-clock time even when a grab was slow.
    * Frames go through a bounded queue to a writer thread, so a slow encoder
      never stalls the capture loop; when the queue is full the frame is
      dropped and counted.
    * ``pause()`` closes the current segment (and its audio); ``resume()``
      starts a new one. ``finish()`` joins segments without re-encoding.
    """

    QUEUE_FRAMES = 30

    def __init__(
        self,
        info: FfmpegInfo,
        output: Path,
        *,
        width: int,
        height: int,
        fps: int,
        quality: int = 23,
        encoder: str = "libx264",
        microphone: str = "",
        system_audio: str = "",
        audio_offset_ms: int = 0,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        runner: Runner = _run,
    ) -> None:
        if width % 2 or height % 2:
            raise ValueError("Frame size must be even for H.264")
        self.info = info
        self.output = output
        self.width, self.height, self.fps = width, height, fps
        self.quality, self.encoder = quality, encoder
        self.microphone, self.system_audio, self.audio_offset_ms = microphone, system_audio, audio_offset_ms
        self._popen, self._runner = popen, runner
        self._segments: list[Path] = []
        self._proc: subprocess.Popen | None = None
        self._queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=self.QUEUE_FRAMES)
        self._writer: threading.Thread | None = None
        self._stderr: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._first_written = threading.Event()
        self._failed: str | None = None
        self._base_slot = 0
        self._next_slot = 0
        self._last: bytes | None = None
        self.frames_written = 0
        self.frames_duplicated = 0
        self.frames_dropped = 0
        self.output.parent.mkdir(parents=True, exist_ok=True)

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * 4

    # -- segment lifecycle ---------------------------------------------------------
    def start(self, ts: float = 0.0) -> None:
        index = len(self._segments) + 1
        segment = self.output.with_name(f"{self.output.stem}.part{index:02d}.mp4")
        args = record_command(
            self.info.path, width=self.width, height=self.height, fps=self.fps, output=segment,
            encoder=self.encoder, quality=self.quality, microphone=self.microphone,
            system_audio=self.system_audio, audio_offset_ms=self.audio_offset_ms,
        )
        log.info("Starting ffmpeg segment %d: %s", index, " ".join(args))
        try:
            self._proc = self._popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=_creation_flags(), bufsize=0,
            )
        except OSError as exc:
            raise RecordingError(f"Cannot start ffmpeg: {exc}", message_key="recording.error.ffmpeg_broken") from exc
        self._segments.append(segment)
        self._queue = queue.Queue(maxsize=self.QUEUE_FRAMES)
        self._first_written.clear()
        self._writer = threading.Thread(target=self._write_loop, args=(self._proc, self._queue), name="ffmpeg-writer", daemon=True)
        self._writer.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, args=(self._proc,), name="ffmpeg-stderr", daemon=True)
        self._stderr_thread.start()
        self._base_slot = int(ts * self.fps)
        self._next_slot = self._base_slot

    def wait_ready(self, timeout: float = 8.0) -> bool:
        """True once ffmpeg accepted the first frame (audio devices are open by then)."""
        return self._first_written.wait(timeout)

    def _write_loop(self, proc: subprocess.Popen, frames: "queue.Queue[bytes | None]") -> None:
        stdin = proc.stdin
        try:
            while True:
                frame = frames.get()
                if frame is None:
                    break
                stdin.write(frame)
                self._first_written.set()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._failed = f"ffmpeg stopped accepting frames: {exc}"
            log.error(self._failed)
            self._first_written.set()
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr.append(line)
                del self._stderr[:-60]
                log.debug("ffmpeg: %s", line)

    def _enqueue(self, frame: bytes) -> None:
        if self._failed:
            raise RecordingError(self._failed + self._stderr_tail(), message_key="recording.error.encoder_failed")
        try:
            self._queue.put_nowait(frame)
            self.frames_written += 1
        except queue.Full:
            self.frames_dropped += 1

    def write(self, frame: bytes, ts: float) -> None:
        if len(frame) != self.frame_bytes:
            raise ValueError(f"Frame has {len(frame)} bytes, expected {self.frame_bytes}")
        slot = int(ts * self.fps)
        if slot < self._next_slot:
            self._last = frame  # faster than the frame rate: keep only the newest frame
            return
        while self._last is not None and self._next_slot < slot:
            self._enqueue(self._last)
            self.frames_duplicated += 1
            self._next_slot += 1
        self._enqueue(frame)
        self._next_slot = slot + 1
        self._last = frame

    def _close_segment(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._queue.put(None)
        if self._writer:
            self._writer.join(FINISH_TIMEOUT)
        try:
            code = proc.wait(FINISH_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.error("ffmpeg did not finish in %ss; terminating", FINISH_TIMEOUT)
            proc.kill()
            code = proc.wait(5)
        if self._stderr_thread:
            self._stderr_thread.join(5)
        if proc.stderr is not None and hasattr(proc.stderr, "close"):
            proc.stderr.close()
        self._proc = None
        if code != 0:
            raise RecordingError(f"ffmpeg exited with code {code}{self._stderr_tail()}", message_key="recording.error.encoder_failed")

    def _stderr_tail(self) -> str:
        return (": " + " | ".join(self._stderr[-5:])) if self._stderr else ""

    def pause(self) -> None:
        self._close_segment()

    def resume(self, ts: float) -> None:
        self._last = None
        self.start(ts)

    def finish(self) -> Path:
        self._close_segment()
        segments = [s for s in self._segments if s.exists() and s.stat().st_size > 0]
        if not segments:
            raise RecordingError("ffmpeg produced no output" + self._stderr_tail(), message_key="recording.error.encoder_failed")
        if len(segments) == 1:
            os.replace(segments[0], self.output)
        else:
            list_file = self.output.with_suffix(".segments.txt")
            list_file.write_text("".join(f"file '{s.as_posix()}'\n" for s in segments), encoding="utf-8")
            proc = self._runner(concat_command(self.info.path, list_file, self.output), timeout=600)
            if proc.returncode != 0:
                raise RecordingError(f"Joining segments failed: {proc.stderr.strip()[-400:]}", message_key="recording.error.encoder_failed")
            for path in segments + [list_file]:
                path.unlink(missing_ok=True)
        return self.output

    def abort(self) -> None:
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
                proc.wait(5)
            except (OSError, subprocess.SubprocessError):
                pass
            self._proc = None
        for path in self._segments:
            for _ in range(3):
                try:
                    path.unlink(missing_ok=True)
                    break
                except PermissionError:  # Windows: file still held for a moment
                    time.sleep(0.2)
