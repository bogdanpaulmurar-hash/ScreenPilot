# Testing — ScreenPilot

Manual test steps per version (what you do → what should happen). Older versions: the checklist at the end of `docs/releases/<version>.md`.

## 0.9.9 — Editor: change step numbers
Update first: close ScreenPilot from the tray → `git pull` → start it again (no reinstall; dependencies unchanged). Covers 0.9.8 too.

1. Make a capture → editor opens → Steps tool (N) → click 3 places → badges 1, 2, 3.
2. Double-click badge 2 → a box asks for the number, it shows 2 → type 7 → OK → badges 1, 7, 8.
3. Right-click badge 1 → **Change number…** → 5 → badges 5, 7, 8.
4. Right-click badge 2 → **Automatic number** → badges 5, 6, 7.
5. Right-click any badge → **Renumber all: 1, 2, 3…** → badges 1, 2, 3. Ctrl+Z → back to 5, 6, 7.
6. Select a badge → Ctrl+D → the copy shows the next free number, not a repeat.
7. Type 123 on a badge → the number fits inside the circle. Copy / Save → the saved image shows the same numbers.
8. In a procedure: Edit image on a step → same behaviour.

## 0.9.8 — Procedure: one image + copy per step
Update first: close ScreenPilot from the tray → `git pull` → start it again (no reinstall; dependencies unchanged).

1. Tray → Procedure → make 3 captures → Finish → the Procedure window opens with 3 steps.
2. Give step 2 a title → Export → **One image (.png)** → save → Explorer opens; the PNG shows the 3 images one below the other, each with "Step N: title" above, in the list's order.
3. Move a step up/down, export again → the order in the PNG follows.
4. Select step 1 → copy button (next to Edit) → "Capture copied" appears → Ctrl+V in Paint/Word pastes step 1.
5. Right-click step 3 in the list → **Copy image** → Ctrl+V pastes step 3.
6. Click step 2 in the list → Ctrl+C → Ctrl+V pastes step 2. Ctrl+C in the title/description fields still copies text.
7. Language Romanian → menu shows "O singură poză (.png)" and "Copiază imaginea".
