# ScreenPilot Roadmap

## 1. Starting point (audit, 2026-10-16)

The repository was empty: no code, no history. ScreenPilot is therefore a
greenfield product. M0 (this release) establishes the architecture and the
headless core; see `docs/ARCHITECTURE.md`.

## 2. Competitive landscape

Positioning: **documentation-first capture for corporate Windows users**, light
enough for old laptops, with nothing that a security team would object to.

| Capability | Snagit | ShareX | Greenshot | Screenpresso | FastStone | ScreenPilot target |
|---|---|---|---|---|---|---|
| Region / window / full / multi-monitor | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ M0 core, M1 UI |
| Scrolling capture | ✓ | ✓ | – | ✓ | ✓ | M2 |
| Step tool / auto numbering | ✓ | basic | – | ✓ | – | M2 (core differentiator) |
| Rich editor & effects | ✓ | ✓ | basic | ✓ | ✓ | M2 |
| OCR | ✓ | ✓ | plugin | ✓ | – | M3, offline, EN/RO/DE |
| Screen recording | ✓ | ✓ (ffmpeg) | – | ✓ | ✓ | M5 |
| Post-capture action chains | limited | ✓ (very rich) | ✓ | ✓ | limited | ✓ M0 core |
| Documentation export (Word/PDF/HTML/MD) | templates | – | – | ✓ (docs) | – | M4 (core differentiator) |
| Workflow / process capture packages | – | – | – | partial | – | M4 (differentiator) |
| UI-element-anchored annotations | – | – | – | – | – | M6 (differentiator) |
| No telemetry by default, portable | ✗ (commercial, account) | ✓ | ✓ | partial | ✓ | ✓ |
| Romanian UI | – | ✓ (community) | ✓ | – | – | ✓ from day one |
| License cost | paid | free OSS | free OSS | freemium | paid | internal |

*Based on publicly documented feature sets; to be re-validated hands-on in M1.*

### Gap analysis — where we can win

1. **Documentation workflow end-to-end**: capture → steps → annotated doc →
   Word/PDF in one flow. Competitors stop at the image (except Snagit templates).
2. **Enterprise trust**: zero telemetry, no keyboard hooks, minimal deps,
   portable, auditable source.
3. **Automation API** (Autopilot) — none of the competitors exposes a clean
   in-process API.
4. **Romanian-first localization** for the local corporate market.

### Where we must reach parity before anyone switches

Region capture UX (magnifier, crosshair, snapping), editor quality, scrolling
capture reliability, hotkey ergonomics.

## 3. Milestones

| Milestone | Content | Exit criteria |
|---|---|---|
| **M0 Foundation** ✅ | core, i18n EN/RO, settings, capture engine, hotkeys, workflow, CLI, Windows CI | tests green on Windows CI |
| **M1 Desktop shell** | tray app, region selector overlay (magnifier, crosshair, window snapping, pixel coordinates), delayed capture countdown, settings window with live language switch, hotkey manager UI, first-run wizard, capture history | daily-usable replacement for Snipping Tool |
| **M2 Editor + Steps** | annotation document model (arrows, shapes, text, callouts, step numbers, blur/pixelate, spotlight, crop), step capture, scrolling capture | can document a 10-step procedure |
| **M3 OCR** | `OcrEngine` protocol, Windows OCR backend (EN/RO/DE), confidence, review, search, OCR→clipboard/TXT/MD | Screenshot→OCR→Clipboard in ≤ 2 clicks |
| **M4 Documentation** | workflow capture sessions, document model, exports PDF/DOCX/HTML/MD, templates, help center, tutorials, tester mode, samples | one-click procedure document |
| **M5 Recording** | screen/window/region recording, mic/system audio, cursor highlight, click effects | 1080p30 on a 4-year-old laptop |
| **M6 Intelligence** | UI element detection, anchored annotations, AI extension layer, Autopilot integration | anchor callouts to detected buttons |

## 4. Quick wins (next, in order)

1. Region selector overlay with magnifier and window snapping (M1 core UX).
2. Tray icon + global hotkeys wired to `CaptureService` and the workflow.
3. Settings window with live EN/RO switch (already supported by the core).
4. Capture history (last N captures, re-copy / re-open).
5. Windows OCR backend (no dependency) — high value, low cost.
6. Cursor inclusion option in captures.

## 5. Risks

| Risk | Mitigation |
|---|---|
| UI cannot be tested in the cloud dev environment | Core is UI-free and tested; Windows CI; hands-on tester checklists per release |
| Package size with Qt | `PySide6-Essentials`, lazy imports, exclude unused Qt modules when packaging |
| Scrolling capture reliability across apps | Combine auto-scroll + image stitching with overlap detection; manual fallback |
| Recording performance in Python | Delegate encoding to Windows Media Foundation or a bundled ffmpeg build (to be reviewed) |
