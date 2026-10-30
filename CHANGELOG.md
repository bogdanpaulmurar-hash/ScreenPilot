# Changelog

Per-release details: `docs/releases/<version>.md`.

## 0.9.7 — 2026-10-30
- Editor: "Copy & close" also saves the edited image to the captures folder (same name pattern, appears in history); new setting `editor.copy_close_saves` (on by default; Settings → "Save on Copy & close").
- Tests (Linux, offscreen Qt): 387 passed, 9 skipped (Windows-only). New: copy & close saves; setting off saves nothing.

## 0.9.6 — 2026-10-27
- Scrolling capture: the block of a floating box at the end of the picture now comes from the last frame, so no faint "shadow" of another section remains around it.
- Tests (Linux, offscreen Qt): 386 passed, 9 skipped (Windows-only). New test fails on 0.9.5.

## 0.9.5 — 2026-10-27
- Scrolling capture: a floating box over the page (chat input) is detected and kept once at the end instead of being pasted at every step; a blinking caret no longer makes every step wait for the view to settle.
- Tests (Linux, offscreen Qt): 385 passed, 9 skipped (Windows-only). New: `FloatingBoxScrollingTests` (+ caret tests); 7 fail on 0.9.4. Reproduced in simulation only, not yet on the real VS Code panel.

## 0.9.4 — 2026-10-27
- Scrolling capture: no repeated rows in Explorer-like lists (hover highlight under the mouse, partial last scroll); manual mode no longer skips those frames. Reported on the laptop with an Explorer folder.
- Tests (Linux, offscreen Qt): 381 passed, 9 skipped (Windows-only). New: `FileListScrollingTests` (2 tests, 14 cases; they fail on 0.9.3).

## 0.9.3 — 2026-10-27
- Selector: with several areas (Ctrl), a switch on the action bar (and key M) chooses "one by one" or "one image" for the clipboard right before Enter; remembered in `capture.multi_clipboard`, disabled when locked by policy.
- Tests (Linux, offscreen Qt): 379 passed, 9 skipped (Windows-only). New: 3 tests in `MultiCaptureUiTests`.

## 0.9.2 — 2026-10-27
- Scrolling capture: OK button on the progress pill finishes the capture (automatic and manual mode), same as Esc but without the pause question. The pill never activates or takes focus (`WS_EX_NOACTIVATE`), stays outside the area and is excluded from capture. Resting the mouse on it pauses automatic scrolling. Esc keeps working.
- Tests (Linux, offscreen Qt): 376 passed, 9 skipped (Windows-only). New: `OkButtonTests` (5, headless) and 3 UI tests in `ScrollingUiTests`. Windows behaviour (focus, capture exclusion) to be checked with the checklist in `docs/releases/0.9.2.md`.
