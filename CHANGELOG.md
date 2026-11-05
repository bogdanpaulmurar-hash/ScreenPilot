# Changelog

Per-release details: `docs/releases/<version>.md`.

## 0.9.9 — 2026-11-05
- Editor: step numbers can be changed. Double-click a step badge (or right-click → "Change number…") and type a number; the next steps continue from it, so the first step sets where the numbering starts. Right-click also offers "Automatic number" and "Renumber all: 1, 2, 3…". A duplicated step counts on instead of repeating a fixed number. Up to 999.
- Tests (Linux, offscreen Qt): 394 passed, 9 skipped (Windows-only). New: hand-set numbers in `EditorDocument` (+ 3-digit render) and `test_step_numbers_can_be_changed` (double-click, start number, undo, duplicate, menu, renumber).

## 0.9.8 — 2026-11-05
- Procedure: new export "One image (.png)" puts every step image into one picture, top to bottom, each with a "Step N: title" line above it.
- Procedure: copy the image of any step to the clipboard (copy button under the preview, right-click on the step in the list, or Ctrl+C in the list).
- Tests (Linux, offscreen Qt): 391 passed, 9 skipped (Windows-only). New: `SingleImageExportTests` (4); the procedure UI test also exports PNG and copies with the button and Ctrl+C.

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
