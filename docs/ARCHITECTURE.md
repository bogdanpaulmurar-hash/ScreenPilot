# ScreenPilot Architecture

Status: M1 (desktop shell) — October 2026.

## Goals that drive the design

1. **Lightweight and enterprise-safe** — minimal third-party code, no telemetry,
   no network access by default, portable mode.
2. **Headless core, optional UI** — every capability is usable without Qt, so
   automation clients (Autopilot) and the CLI share the exact same engines.
3. **Loose coupling** — engines talk through an event bus and a service
   registry, never through each other's internals.
4. **Localization from day one** — no user-facing string in code.

## Layers

```
┌────────────────────────────────────────────────────────────┐
│  Clients:  Desktop UI (Qt)  ·  CLI  ·  Autopilot           │
├────────────────────────────────────────────────────────────┤
│  Facade:   screenpilot.app.create_app() -> ScreenPilotApp  │
│            (composition root + ServiceRegistry)            │
├──────────────┬──────────────┬──────────────┬───────────────┤
│ Capture      │ Workflow     │ Hotkeys      │ Settings·i18n │
│ Engine       │ Engine       │ Manager      │               │
│ (+Recording, │ (actions,    │              │               │
│  OCR, Editor,│  exports)    │              │               │
│  Docs - next)│              │              │               │
├──────────────┴──────────────┴──────────────┴───────────────┤
│  Core: EventBus · ServiceRegistry · errors · paths · logs  │
├────────────────────────────────────────────────────────────┤
│  Platform: Win32 via ctypes (GDI, RegisterHotKey,          │
│            clipboard, shell) — behind small Protocols      │
└────────────────────────────────────────────────────────────┘
```

## Packages

| Package | Responsibility | Status |
|---|---|---|
| `core` | events, service registry, errors, paths, logging | done |
| `i18n` | JSON catalogs, live language switching, fallback | done |
| `settings` | defaults, validation, atomic save, migrations | done |
| `capture` | geometry, image model, backends, `CaptureService` | done (static) |
| `hotkeys` | parsing, bindings, conflicts, diagnostics, profiles | done |
| `workflow` | chained post-capture actions | done (core actions) |
| `platform` | clipboard (DIB + PNG), Explorer reveal, focus, Esc key latch | done (Windows) |
| `ui` | Qt tray, region selector, countdown, settings, hotkeys, history, wizard | done |
| `history` | recent captures + thumbnails | done |
| `samples` | synthetic demo desktop for demos, tutorials, tests | done |
| `editor` | annotation document model, steps, watermark + Qt canvas | done (v1) |
| `ocr` | `OcrEngine` protocol; Windows.Media.Ocr, Tesseract; sensitive data | done (v1) |
| `docs` | procedure sessions; DOCX/HTML/MD export (PDF via Qt) | done (v1) |
| `recording` | frame source → effects (ink, cursor, clicks, zoom) → sink (ffmpeg MP4 / Pillow GIF), `RecordingService`; `ink` = live annotations (`InkLayer`, `InkRenderer`) | done (v1 + live annotations) |
| `videoedit` | video projects (trim + timed annotations, JSON), frame readers (FFmpeg seek / Pillow GIF), export (one FFmpeg pass / Pillow GIF), `VideoEditService` = `app.video` | done (v1) |
| `ai` | extension points: detectors, annotators, summarizers | M6 |

## Key decisions

| # | Decision | Why |
|---|---|---|
| ADR-1 | Python 3.10+, `src/` layout | Matches the Python Autopilot; 3.10 still common on corporate images |
| ADR-2 | Win32 through `ctypes` instead of `pywin32`/`mss` | No compiled third-party packages for core features; smaller attack surface and install |
| ADR-3 | GDI `BitBlt` + `CAPTUREBLT` for still captures | Works everywhere, captures layered windows; DXGI/WGC reserved for recording |
| ADR-4 | Per-monitor DPI awareness v2 | Pixel-exact captures on mixed-DPI multi-monitor setups |
| ADR-5 | `RegisterHotKey` instead of keyboard hooks | Low privilege, not flagged by EDR, clean conflict signal |
| ADR-6 | Pixels kept as raw BGRA; Pillow only on save/edit | Fast capture path, low memory |
| ADR-7 | Workflow steps are JSON specs | Users, presets and Autopilot can chain actions without code |
| ADR-8 | PySide6 (LGPL) for the UI, optional extra | Mature, official Qt binding; LGPL is enterprise-friendly when dynamically linked |
| ADR-9 | Flat JSON locale files | Diff-friendly, translators need no tooling, new language = new file |
| ADR-10 | Recording: ScreenPilot grabs frames, ffmpeg only encodes (stdin pipe) | Effects identical for MP4 and GIF; ffmpeg stays an optional, replaceable external process (no linking, IT chooses the build) |
| ADR-11 | Constant frame rate by repeating frames; pause = new segment | Audio/video stay in sync with wall-clock time; segments join without re-encoding |
| ADR-12 | GIF streamed to disk as changed-area patches (Pillow encodes each patch; ScreenPilot writes the GIF container) | Full resolution with no memory cap; screen content (≤256 colours per patch) is lossless; cancelled recordings leave only a `.part` file that is removed |
| ADR-13 | Frame detection has a time budget (25 ms) and visits side combinations nearest-first | Grids (Excel, Sheets) made the exhaustive search take seconds per mouse move and froze the selector |
| ADR-14 | Esc outside our windows = polling one key (`GetAsyncKeyState`) only while a flow runs | Same no-hook rule as hotkeys; works when another app keeps the keyboard focus |
| ADR-15 | "FFmpeg not found" is never cached; discovery also checks winget/Chocolatey/Scoop folders and the registry `Path` | An FFmpeg installed while the app runs (stale process `PATH`) is used at the next recording without a restart |
| ADR-16 | Captures are drawn at most 1 image px per *physical* screen px (`capture.geometry.fit_view`, `ui.qt_helpers.fit_target` / `sharp_thumbnail`) | Capping at 1 *logical* px upscaled every capture by 125–150 % on scaled screens, which made text look soft in the editor, Number steps, OCR and previews |
| ADR-19 | Video editing exports in one FFmpeg pass: input seeking + re-encode for a frame-exact cut, each annotation a small PNG input with `overlay … enable='between(t,a,b)'` (BT.709 conversion), blur = `crop`+`boxblur` overlay; GIFs are edited with Pillow | No second tool and no frame-by-frame Python loop for MP4 (about real time or faster with a hardware encoder); a stream-copy cut would snap to keyframes (every ~2 s in our recordings); GIF editing works without FFmpeg; the source file is never modified (`<name>_edited`, `.part` + rename) |
| ADR-18 | Live annotations are burned in by the recorder (`recording.ink`); the Qt overlay is only an input/preview surface excluded from capture | Headless: Autopilot can add steps with `recorder.ink.add_step(...)`; same Pillow renderer as the editor; per-item cached BGRA patches keep the per-frame cost tied to the annotation size (≈0.3 ms per step at 1080p), zero without ink. Without `WDA_EXCLUDEFROMCAPTURE` the overlay itself is recorded and `burn_in` is switched off |
| ADR-17 | MP4: explicit RGB→YUV conversion with BT.709, accurate rounding and colour tags (`SCREEN_COLOR_FILTER`, `COLOR_TAGS`) in one `-filter_complex` with the audio mix | ffmpeg's implicit conversion uses BT.601 untagged, so players shift colours and coloured text bleeds more |

## Events (public)

| Event | Payload |
|---|---|
| `capture.completed` | `result: CaptureResult` |
| `capture.failed` | `mode`, `error` |
| `capture.countdown` | `seconds` |
| `capture.cancelled` | `mode` (optional; `scrolling` when the user discards a scrolling capture) |
| `editor.open_requested` | `result`, `path` |
| `workflow.finished` | `results`, `path` |
| `recording.state` | `state`, `elapsed` |
| `recording.finished` | `result: RecordingResult` |
| `recording.failed` | `error` |
| `video.exported` | `path`, `source`, `duration` |
| `recording.saved` | `path`, `format`, `duration` |

## Autopilot integration

```python
from screenpilot.app import create_app
from screenpilot.capture import Rect

app = create_app()
result = app.capture.capture_region(Rect(100, 100, 800, 600))
steps, ctx = app.run_after_capture(result, [{"id": "save"}])
app.bus.subscribe("capture.completed", lambda e: ...)

recorder = app.recording.start(Rect(0, 0, 1280, 720))  # MP4 or GIF per settings
...
result = recorder.stop(wait=True)                      # RecordingResult(path, duration, ...)
```

Only `screenpilot.app`, the engine services and the event names above are
considered stable API.

## Threading

Captures are synchronous and fast (tens of ms); the UI runs them on a worker
thread. Event handlers run on the publisher's thread — UI subscribers marshal
to the GUI thread themselves. The hotkey listener owns its own Win32 message
thread.

The scrolling capture worker can block on a question (Esc → continue / stop /
discard): it emits a Qt signal with a `threading.Event`, the GUI thread shows
the dialog and sets the answer.

UI flows (selector, countdowns, recording, interval capture) poll Esc every
60 ms with a `KeyLatch` (one event per press) while one of them is active; the
timer stops by itself afterwards. The selector overlay takes focus with
`AttachThreadInput` + `SetForegroundWindow`, because Windows may refuse a
plain activation for a window shown from a hotkey while Excel or a browser is
in front. Window ghosting is disabled for the process.

## Testing

* Pure-Python tests run anywhere with `FakeBackend` (deterministic pixels).
* `tests/test_win32_integration.py` runs against real Windows in CI.
* Locale parity (keys and placeholders) is enforced by tests.
