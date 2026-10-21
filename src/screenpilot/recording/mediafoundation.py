"""Built-in MP4 (H.264) writer using Windows Media Foundation — no external program.

Why: on managed corporate laptops security software can refuse to start an
unsigned ``ffmpeg.exe`` or kill it while it receives screen frames. Media
Foundation is part of Windows (signed system DLLs, in-process), uses the
hardware H.264 encoder when there is one (Intel Quick Sync, NVIDIA, AMD) and
falls back to Microsoft's software encoder.

Implementation: the Sink Writer (``MFCreateSinkWriterFromURL``) through ctypes
COM vtable calls. Frames are top-down BGRA (``MFVideoFormat_RGB32`` with a
positive default stride); the Sink Writer converts them to NV12 for the
encoder. All Media Foundation calls run on one dedicated thread (COM MTA).
Video only: sound still needs FFmpeg.

COM vtable indices (from the Windows SDK headers):

* ``IMFAttributes``: SetUINT32 21, SetUINT64 22, SetGUID 24 (``IMFMediaType``
  and ``IMFSample`` derive from it).
* ``IMFSample``: SetSampleTime 36, SetSampleDuration 38, AddBuffer 42.
* ``IMFMediaBuffer``: Lock 3, Unlock 4, SetCurrentLength 6.
* ``IMFSinkWriter``: AddStream 3, SetInputMediaType 4, BeginWriting 5,
  WriteSample 6, Finalize 11.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import uuid
from pathlib import Path

from screenpilot.core.errors import RecordingError

log = logging.getLogger(__name__)

ENCODER_NAME = "windows-h264"
MF_VERSION = 0x00020070
COINIT_MULTITHREADED = 0x0
HUNDRED_NS = 10_000_000
QUEUE_FRAMES = 30
MAX_BITRATE = 50_000_000


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


MF_MT_MAJOR_TYPE = _guid("48eba18e-f8c9-4687-bf11-0a74c9f96a8f")
MF_MT_SUBTYPE = _guid("f7e34c9a-42e8-4714-b74b-cb29d72c35e5")
MF_MT_AVG_BITRATE = _guid("20332624-fb0d-4d9e-bd0d-cbf6786c102e")
MF_MT_INTERLACE_MODE = _guid("e2724bb8-e676-4806-b4b2-a8d6efb44ccd")
MF_MT_FRAME_SIZE = _guid("1652c33d-d6b2-4012-b834-72030849a37d")
MF_MT_FRAME_RATE = _guid("c459a2e8-3d2c-4e44-b132-fee5156c7bb0")
MF_MT_PIXEL_ASPECT_RATIO = _guid("c6376a1e-8d0a-4027-be45-6d9a0ad39bb6")
MF_MT_DEFAULT_STRIDE = _guid("644b4e48-1e02-4516-b0eb-c01ca9d49ac6")
MF_MT_MPEG2_PROFILE = _guid("ad76a80b-2d5c-4e0b-b375-64e520137036")
MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS = _guid("a634a91c-822b-41b9-a494-4de4643612b0")
MFMediaType_Video = _guid("73646976-0000-0010-8000-00aa00389b71")
MFVideoFormat_H264 = _guid("34363248-0000-0010-8000-00aa00389b71")
MFVideoFormat_RGB32 = _guid("00000016-0000-0010-8000-00aa00389b71")
MFVideoInterlace_Progressive = 2
eAVEncH264VProfile_High = 100

HRESULT = ctypes.c_long


def _hr(value: int, what: str) -> None:
    if value < 0:
        raise RecordingError(
            f"Media Foundation: {what} failed (0x{value & 0xFFFFFFFF:08X})", message_key="recording.error.windows_encoder"
        )


def _call(obj: ctypes.c_void_p, index: int, argtypes: tuple, *args, what: str = "") -> int:
    """Call COM method ``index`` of ``obj`` (first argument ``this``) and check the HRESULT."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])  # type: ignore[attr-defined]
    hr = fn(obj, *args)
    _hr(hr, what or f"method {index}")
    return hr


def _release(obj: ctypes.c_void_p | None) -> None:
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)  # type: ignore[attr-defined]


def _set_u32(obj, key: GUID, value: int) -> None:
    _call(obj, 21, (ctypes.POINTER(GUID), ctypes.c_uint32), ctypes.byref(key), ctypes.c_uint32(value & 0xFFFFFFFF), what="SetUINT32")


def _set_u64(obj, key: GUID, value: int) -> None:
    _call(obj, 22, (ctypes.POINTER(GUID), ctypes.c_uint64), ctypes.byref(key), ctypes.c_uint64(value), what="SetUINT64")


def _set_guid(obj, key: GUID, value: GUID) -> None:
    _call(obj, 24, (ctypes.POINTER(GUID), ctypes.POINTER(GUID)), ctypes.byref(key), ctypes.byref(value), what="SetGUID")


class _MF:
    """Loaded system DLLs (Windows only)."""

    def __init__(self) -> None:
        self.ole32 = ctypes.WinDLL("ole32")  # type: ignore[attr-defined]
        self.mfplat = ctypes.WinDLL("mfplat")  # type: ignore[attr-defined]
        self.mfreadwrite = ctypes.WinDLL("mfreadwrite")  # type: ignore[attr-defined]
        self.mfplat.MFStartup.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        self.mfplat.MFStartup.restype = HRESULT
        self.mfplat.MFShutdown.restype = HRESULT
        for name in ("MFCreateMediaType", "MFCreateSample"):
            getattr(self.mfplat, name).argtypes = [ctypes.POINTER(ctypes.c_void_p)]
            getattr(self.mfplat, name).restype = HRESULT
        self.mfplat.MFCreateAttributes.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
        self.mfplat.MFCreateAttributes.restype = HRESULT
        self.mfplat.MFCreateMemoryBuffer.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        self.mfplat.MFCreateMemoryBuffer.restype = HRESULT
        self.mfreadwrite.MFCreateSinkWriterFromURL.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        self.mfreadwrite.MFCreateSinkWriterFromURL.restype = HRESULT
        self.ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.ole32.CoInitializeEx.restype = HRESULT


def bitrate_for(width: int, height: int, fps: int, quality: int) -> int:
    """Same curve as FFmpeg's bitrate-based encoders: ~7.5 Mbit/s for 1080p30 at quality 23."""
    return max(500_000, min(MAX_BITRATE, int(width * height * fps * 0.12 * (23 / max(quality, 1)))))


class _Writer:
    """One MP4 file written by a Sink Writer; every method runs on the owner thread."""

    def __init__(self, mf: _MF, output: Path, width: int, height: int, fps: int, bitrate: int) -> None:
        self.mf = mf
        self.width, self.height, self.fps = width, height, fps
        self.frame_bytes = width * height * 4
        self.writer = ctypes.c_void_p()
        self.stream = ctypes.c_ulong()
        attrs, out_type, in_type = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        try:
            _hr(mf.mfplat.MFCreateAttributes(ctypes.byref(attrs), 2), "MFCreateAttributes")
            _set_u32(attrs, MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, 1)
            _hr(mf.mfreadwrite.MFCreateSinkWriterFromURL(str(output), None, attrs, ctypes.byref(self.writer)),
                "MFCreateSinkWriterFromURL")

            _hr(mf.mfplat.MFCreateMediaType(ctypes.byref(out_type)), "MFCreateMediaType")
            _set_guid(out_type, MF_MT_MAJOR_TYPE, MFMediaType_Video)
            _set_guid(out_type, MF_MT_SUBTYPE, MFVideoFormat_H264)
            _set_u32(out_type, MF_MT_AVG_BITRATE, bitrate)
            _set_u32(out_type, MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive)
            _set_u32(out_type, MF_MT_MPEG2_PROFILE, eAVEncH264VProfile_High)
            _set_u64(out_type, MF_MT_FRAME_SIZE, (width << 32) | height)
            _set_u64(out_type, MF_MT_FRAME_RATE, (fps << 32) | 1)
            _set_u64(out_type, MF_MT_PIXEL_ASPECT_RATIO, (1 << 32) | 1)
            _call(self.writer, 3, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)), out_type, ctypes.byref(self.stream),
                  what="AddStream")

            _hr(mf.mfplat.MFCreateMediaType(ctypes.byref(in_type)), "MFCreateMediaType")
            _set_guid(in_type, MF_MT_MAJOR_TYPE, MFMediaType_Video)
            _set_guid(in_type, MF_MT_SUBTYPE, MFVideoFormat_RGB32)
            _set_u32(in_type, MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive)
            _set_u32(in_type, MF_MT_DEFAULT_STRIDE, width * 4)  # positive stride = top-down rows, like GDI DIB sections here
            _set_u64(in_type, MF_MT_FRAME_SIZE, (width << 32) | height)
            _set_u64(in_type, MF_MT_FRAME_RATE, (fps << 32) | 1)
            _set_u64(in_type, MF_MT_PIXEL_ASPECT_RATIO, (1 << 32) | 1)
            _call(self.writer, 4, (ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p), self.stream, in_type, None,
                  what="SetInputMediaType")
            _call(self.writer, 5, (), what="BeginWriting")
        except Exception:
            _release(self.writer)
            self.writer = ctypes.c_void_p()
            raise
        finally:
            for obj in (attrs, out_type, in_type):
                _release(obj)

    def write(self, frame: bytes, ts: float, duration: float) -> None:
        buf, sample = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            _hr(self.mf.mfplat.MFCreateMemoryBuffer(self.frame_bytes, ctypes.byref(buf)), "MFCreateMemoryBuffer")
            data = ctypes.c_void_p()
            _call(buf, 3, (ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p), ctypes.byref(data), None, None,
                  what="Lock")
            try:
                ctypes.memmove(data, frame, self.frame_bytes)
            finally:
                _call(buf, 4, (), what="Unlock")
            _call(buf, 6, (ctypes.c_ulong,), self.frame_bytes, what="SetCurrentLength")
            _hr(self.mf.mfplat.MFCreateSample(ctypes.byref(sample)), "MFCreateSample")
            _call(sample, 42, (ctypes.c_void_p,), buf, what="AddBuffer")
            _call(sample, 36, (ctypes.c_longlong,), int(ts * HUNDRED_NS), what="SetSampleTime")
            _call(sample, 38, (ctypes.c_longlong,), max(1, int(duration * HUNDRED_NS)), what="SetSampleDuration")
            _call(self.writer, 6, (ctypes.c_ulong, ctypes.c_void_p), self.stream, sample, what="WriteSample")
        finally:
            _release(sample)
            _release(buf)

    def finalize(self) -> None:
        try:
            _call(self.writer, 11, (), what="Finalize")
        finally:
            self.close()

    def close(self) -> None:
        _release(self.writer)
        self.writer = ctypes.c_void_p()


class MediaFoundationSink:
    """Recorder sink (same protocol as ``FfmpegSink``) that writes MP4 with Windows' own encoder.

    Timestamps come from the recorder's timeline (paused time already removed), so pause and
    resume need no segments. Frames go through a bounded queue to the Media Foundation
    thread; when the encoder cannot keep up, frames are dropped (and counted), never the
    capture loop blocked.
    """

    def __init__(self, output: Path, *, width: int, height: int, fps: int, quality: int = 20) -> None:
        if width % 2 or height % 2:
            raise ValueError("Frame size must be even for H.264")
        self.output = output
        self.width, self.height, self.fps = width, height, fps
        self.bitrate = bitrate_for(width, height, fps, quality)
        self.frames_written = 0
        self.frames_dropped = 0
        self.frames_duplicated = 0
        self._queue: "queue.Queue[tuple[bytes, float] | None]" = queue.Queue(maxsize=QUEUE_FRAMES)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._done = threading.Event()
        self._failed: RecordingError | None = None
        self._abort = False
        self._last_ts: float | None = None
        output.parent.mkdir(parents=True, exist_ok=True)

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * 4

    # -- owner thread ------------------------------------------------------------------
    def _run(self) -> None:
        mf = None
        writer = None
        started = False
        try:
            mf = _MF()
            mf.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
            _hr(mf.mfplat.MFStartup(MF_VERSION, 0), "MFStartup")
            started = True
            writer = _Writer(mf, self.output, self.width, self.height, self.fps, self.bitrate)
            log.info("Windows H.264 encoder ready (%dx%d, %d fps, %.1f Mbit/s)", self.width, self.height, self.fps, self.bitrate / 1e6)
            self._ready.set()
            pending: tuple[bytes, float] | None = None
            while True:
                item = self._queue.get()
                if item is None:
                    break
                if pending is not None:  # each frame lasts until the next one starts
                    writer.write(pending[0], pending[1], max(item[1] - pending[1], 1 / self.fps))
                pending = item
            if pending is not None and not self._abort:
                writer.write(pending[0], pending[1], 1 / self.fps)
            if self._abort:
                writer.close()
            else:
                writer.finalize()
            writer = None
        except RecordingError as exc:
            self._failed = exc
            log.error("%s", exc)
        except Exception as exc:  # noqa: BLE001 - any COM / ctypes failure ends the recording cleanly
            log.exception("Windows encoder failed")
            self._failed = RecordingError(f"Windows encoder failed: {exc}", message_key="recording.error.windows_encoder")
        finally:
            if writer is not None:
                writer.close()
            if mf is not None and started:
                mf.mfplat.MFShutdown()
            self._ready.set()
            self._done.set()

    # -- sink protocol (recorder thread) -------------------------------------------------
    def start(self, ts: float = 0.0) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="mf-writer", daemon=True)
            self._thread.start()

    def wait_ready(self, timeout: float = 8.0) -> bool:
        ok = self._ready.wait(timeout)
        if self._failed is not None:
            raise self._failed
        return ok

    def write(self, frame: bytes, ts: float) -> None:
        if self._failed is not None:
            raise self._failed
        if len(frame) != self.frame_bytes:
            raise ValueError(f"Frame has {len(frame)} bytes, expected {self.frame_bytes}")
        if self._last_ts is not None and ts <= self._last_ts:
            return  # timestamps must increase
        try:
            self._queue.put_nowait((frame, ts))
            self._last_ts = ts
            self.frames_written += 1
        except queue.Full:
            self.frames_dropped += 1

    def pause(self) -> None:  # timestamps exclude paused time: nothing to do
        pass

    def resume(self, ts: float) -> None:
        pass

    def finish(self) -> Path:
        self._queue.put(None)
        self._done.wait(120)
        if self._failed is not None:
            raise self._failed
        if not self.output.exists() or self.output.stat().st_size == 0:
            raise RecordingError("The Windows encoder produced no video", message_key="recording.error.windows_encoder")
        return self.output

    def abort(self) -> None:
        self._abort = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._queue.put_nowait(None)
        self._done.wait(30)
        try:
            self.output.unlink(missing_ok=True)
        except OSError:
            pass


_available: tuple[bool, str] | None = None
_lock = threading.Lock()


def availability(probe_dir: Path | None = None) -> tuple[bool, str]:
    """Can this PC write H.264 MP4 through Media Foundation? Checked once (tiny test file)."""
    global _available
    if sys.platform != "win32":
        return False, "Windows only"
    with _lock:
        if _available is not None:
            return _available
        import tempfile

        folder = Path(probe_dir or tempfile.gettempdir())
        target = folder / f"screenpilot-mf-probe-{uuid.uuid4().hex[:8]}.mp4"
        sink = MediaFoundationSink(target, width=64, height=64, fps=10)
        try:
            sink.start()
            if not sink.wait_ready(15):
                raise RecordingError("no answer", message_key="recording.error.windows_encoder")
            frame = bytes([128, 128, 128, 255]) * (64 * 64)
            for i in range(3):
                sink.write(frame, i / 10)
            sink.finish()
            _available = (True, "")
        except Exception as exc:  # noqa: BLE001
            _available = (False, str(exc))
        finally:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        log.info("Windows H.264 encoder: %s", "available" if _available[0] else f"not available ({_available[1]})")
        return _available


def reset_availability() -> None:
    global _available
    with _lock:
        _available = None


__all__ = ["ENCODER_NAME", "MediaFoundationSink", "availability", "bitrate_for", "reset_availability"]
