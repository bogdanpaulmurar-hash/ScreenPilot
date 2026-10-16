# ScreenPilot Architecture

Status: M0 (foundation) — October 2026.

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
│  Clients:  Desktop UI (Qt, M1)  ·  CLI  ·  Autopilot       │
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
| `platform` | clipboard, shell reveal | done (Windows) |
| `ui` | Qt shell, region selector, tray, settings, wizard | M1 |
| `editor` | annotation document model + Qt canvas | M2 |
| `ocr` | `OcrEngine` protocol; Windows.Media.Ocr, Tesseract | M3 |
| `docs_engine` | step/workflow documents; PDF/DOCX/HTML/MD export | M4 |
| `recording` | screen/window/region video, audio, effects | M5 |
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

## Events (public)

| Event | Payload |
|---|---|
| `capture.completed` | `result: CaptureResult` |
| `capture.failed` | `mode`, `error` |
| `capture.countdown` | `seconds` |
| `capture.cancelled` | — |
| `editor.open_requested` | `result`, `path` |
| `workflow.finished` | `results`, `path` |

## Autopilot integration

```python
from screenpilot.app import create_app
from screenpilot.capture import Rect

app = create_app()
result = app.capture.capture_region(Rect(100, 100, 800, 600))
steps, ctx = app.run_after_capture(result, [{"id": "save"}])
app.bus.subscribe("capture.completed", lambda e: ...)
```

Only `screenpilot.app`, the engine services and the event names above are
considered stable API.

## Threading

Captures are synchronous and fast (tens of ms); the UI runs them on a worker
thread. Event handlers run on the publisher's thread — UI subscribers marshal
to the GUI thread themselves. The hotkey listener owns its own Win32 message
thread.

## Testing

* Pure-Python tests run anywhere with `FakeBackend` (deterministic pixels).
* `tests/test_win32_integration.py` runs against real Windows in CI.
* Locale parity (keys and placeholders) is enforced by tests.
