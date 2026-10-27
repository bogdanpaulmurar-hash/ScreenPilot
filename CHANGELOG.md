# Changelog

Per-release details: `docs/releases/<version>.md`.

## 0.9.2 — 2026-10-27
- Scrolling capture: OK button on the progress pill finishes the capture (automatic and manual mode), same as Esc but without the pause question. The pill never activates or takes focus (`WS_EX_NOACTIVATE`), stays outside the area and is excluded from capture. Resting the mouse on it pauses automatic scrolling. Esc keeps working.
- Tests (Linux, offscreen Qt): 376 passed, 9 skipped (Windows-only). New: `OkButtonTests` (5, headless) and 3 UI tests in `ScrollingUiTests`. Windows behaviour (focus, capture exclusion) to be checked with the checklist in `docs/releases/0.9.2.md`.
