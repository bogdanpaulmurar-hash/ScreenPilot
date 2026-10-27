# Changelog

Per-release details: `docs/releases/<version>.md`.

## 0.9.3 — 2026-10-27
- Selector: with several areas (Ctrl), a switch on the action bar (and key M) chooses "one by one" or "one image" for the clipboard right before Enter; remembered in `capture.multi_clipboard`, disabled when locked by policy.
- Tests (Linux, offscreen Qt): 379 passed, 9 skipped (Windows-only). New: 3 tests in `MultiCaptureUiTests`.

## 0.9.2 — 2026-10-27
- Scrolling capture: OK button on the progress pill finishes the capture (automatic and manual mode), same as Esc but without the pause question. The pill never activates or takes focus (`WS_EX_NOACTIVATE`), stays outside the area and is excluded from capture. Resting the mouse on it pauses automatic scrolling. Esc keeps working.
- Tests (Linux, offscreen Qt): 376 passed, 9 skipped (Windows-only). New: `OkButtonTests` (5, headless) and 3 UI tests in `ScrollingUiTests`. Windows behaviour (focus, capture exclusion) to be checked with the checklist in `docs/releases/0.9.2.md`.
