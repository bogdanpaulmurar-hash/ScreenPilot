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
| **M1 Desktop shell** ✅ | tray app, region selector overlay (magnifier, crosshair, window snapping, pixel coordinates), delayed capture countdown, settings window with live language switch, hotkey manager UI, first-run wizard, capture history | daily-usable replacement for Snipping Tool |
| **M1.5 Modern UI** ✅ | Fluent theme light/dark, Capture Hub, edge capture bar, selector action bar, quick preview with drag-and-drop, pin to screen | looks and feels on par with Snagit |
| **M1.6 Smart capture** ✅ | frame auto-detection with parent levels, scrolling capture (+ capture more), step numbering attached to elements, history size setting | procedures documented in a few clicks |
| **M1.7 Pro tools** ✅ | capture modes (hotkey + border colour + Enter action), timing journal + diagnostics export, colour picker, ruler, interval capture, watermark, IT policy, portable package | ready for a pilot group |
| **M2 Editor + Steps** ✅ (v1) | annotation document model (arrows, shapes, text, callouts, step numbers, blur/pixelate, spotlight, crop), step capture, scrolling capture | can document a 10-step procedure |
| **M3 OCR** ✅ (v1) | `OcrEngine` protocol, Windows OCR backend (EN/RO/DE), confidence, review, search, OCR→clipboard/TXT/MD | Screenshot→OCR→Clipboard in ≤ 2 clicks |
| **M4 Documentation** ✅ (v1: procedures → DOCX/PDF/HTML/MD) | workflow capture sessions, document model, exports PDF/DOCX/HTML/MD, templates, help center, tutorials, tester mode, samples | one-click procedure document |
| **M5 Recording** ✅ (v1) | region/window/screen to MP4 (external ffmpeg, hardware encoders) or GIF (built-in), mic + loopback audio, cursor highlight, click ripples, zoom, pause | 1080p30 on a 4-year-old laptop (to verify on real hardware) |
| **M5.0.1 Pilot feedback** ✅ (0.5.1) | FFmpeg found without restart, full-resolution streamed GIF, PNG on the clipboard, selector fast on spreadsheets + focus fix, Esc everywhere (pause + ask for recording/scrolling), optional action bar, Region "copy only", editor picker (last / 10 recent / clipboard / file / all), Copy & close, Explorer opens the right folder | issues from the first real-use round fixed |
| **M5.1 Recording+** | native WASAPI loopback (no "Stereo Mix"), Windows.Graphics.Capture source for 4K60, webcam overlay, trim editor, keystroke captions (opt-in), **video annotations v1** (step numbers / arrows / text with start–end time, burned in by ffmpeg at export — see below) | Loom-style tutorial in one take |
| **M6 Intelligence** | UI element detection, anchored annotations, AI extension layer, Autopilot integration | anchor callouts to detected buttons |

### Video annotations (proposal, not started)

Two complementary options, both outside the capture loop so recording speed is unchanged:

1. **Live annotations while recording** — a transparent "ink" overlay over the
   recorded area (toggle in the control bar): click to drop a numbered step,
   drag for an arrow or rectangle. The overlay is a normal window, so it is
   simply captured with the frames (no per-frame cost). Works for MP4 and GIF.
2. **Post-recording editor (with the trim editor)** — a timeline where each
   annotation (step, arrow, rectangle, text, blur) has a start and end time.
   Export re-encodes once with ffmpeg (`overlay` filter with `enable='between(t,a,b)'`
   on PNGs rendered by the existing editor renderer), so it needs FFmpeg; a GIF
   can be annotated by rewriting its frames with Pillow. Cost: one re-encode
   at export (about real time on a laptop with a hardware encoder).

## 4. Quick wins (next, in order)

Done in M1: region selector with magnifier and snapping, tray + global
hotkeys, live EN/RO settings, capture history.

1. Windows OCR backend (built into Windows, no dependency) — high value, low cost.
2. Editor v1: arrows, rectangles, text, step numbers, blur, crop.
3. Cursor inclusion option in captures.
4. Packaging: single-folder portable build + installer.

## 5. Risks

| Risk | Mitigation |
|---|---|
| UI cannot be tested in the cloud dev environment | Core is UI-free and tested; Windows CI; hands-on tester checklists per release |
| Package size with Qt | `PySide6-Essentials`, lazy imports, exclude unused Qt modules when packaging |
| Scrolling capture reliability across apps | Combine auto-scroll + image stitching with overlap detection; manual fallback |
| Recording performance in Python | Encoding delegated to an external ffmpeg (hardware encoders); capture loop measured per session (`recording.session` timing); WGC source planned for 4K60 |
| FFmpeg availability on locked-down PCs | Not bundled on purpose; GIF fallback always works; IT can deploy an approved build next to the exe or via policy-locked path |
