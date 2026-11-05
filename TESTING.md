# Testing — ScreenPilot

Manual test steps per version (what you do → what should happen). Older versions: the checklist at the end of `docs/releases/<version>.md`.

## 0.9.8 — Procedure: one image + copy per step
Update first: close ScreenPilot from the tray → `git pull` → start it again (no reinstall; dependencies unchanged).

1. Tray → Procedure → make 3 captures → Finish → the Procedure window opens with 3 steps.
2. Give step 2 a title → Export → **One image (.png)** → save → Explorer opens; the PNG shows the 3 images one below the other, each with "Step N: title" above, in the list's order.
3. Move a step up/down, export again → the order in the PNG follows.
4. Select step 1 → copy button (next to Edit) → "Capture copied" appears → Ctrl+V in Paint/Word pastes step 1.
5. Right-click step 3 in the list → **Copy image** → Ctrl+V pastes step 3.
6. Click step 2 in the list → Ctrl+C → Ctrl+V pastes step 2. Ctrl+C in the title/description fields still copies text.
7. Language Romanian → menu shows "O singură poză (.png)" and "Copiază imaginea".
