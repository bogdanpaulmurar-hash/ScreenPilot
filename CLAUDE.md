# ScreenPilot — notes for Claude

Lightweight, enterprise-friendly screenshot, screen recording and documentation tool for Windows (Python, PySide6 UI, headless core).

## Commands
- Dev install: `pip install -e ".[ui,dev]"`
- Tests: `pytest -q` (on Linux: `QT_QPA_PLATFORM=offscreen pytest -q`); CI runs on Windows with ffmpeg
- User install: `Install.bat` (runs `scripts\install.ps1 -Autostart -Ffmpeg -DesktopShortcut`); clone outside Google Drive / OneDrive
- Run: `python -m screenpilot`
- Version: `src/screenpilot/__init__.py` (`__version__`) and `pyproject.toml` — bump both; add `docs/releases/<version>.md`

## Map (`src/screenpilot/`)
`core` (headless API for automation, e.g. AutoPilot) · `capture` · `editor` · `ocr` · `recording` · `videoedit` · `workflow` · `docs` (procedure documents) · `hotkeys` · `settings` · `i18n` · `platform` (Win32) · `ui` · `app`
Docs: `docs/ARCHITECTURE.md`, `docs/DEPENDENCIES.md`, `docs/ROADMAP.md`.

## Rules
- No `.exe` for the user (office antivirus deletes them): Python, started with `pythonw`, tray, start with Windows, no console. The nightly portable build is optional only.
- Must not interfere with the many other Python apps running in parallel (single-instance lock, no global keyboard hooks).
- Enterprise-safe: no telemetry, no network access, minimal dependencies — justify every new one in `docs/DEPENDENCIES.md`.
- The core must stay importable without Qt (the `ui` extra is optional).
- Every user-facing text goes through i18n, English and Romanian.
- Integrated in DevPilot via `devpilot.tool.json`; respect `DEVPILOT_MANAGED` / `DEVPILOT_SUPPRESS`.
- Code, comments, commits and technical docs in English; progress updates to the user in Romanian, short and phone-friendly.
- Visible docs: never the employer's name, company e-mails, internal IDs or numbers.

## Shared context
Project overview and open bugs/changes live in two Google Docs: **PROIECTE_CONTEXT (live)** and **MODIFICARI_PROIECTE (live)** — read only the ScreenPilot section. Remove items from MODIFICARI once implemented and confirmed.

## Keep this file current
Update it in the same commit when: install/test/run commands change, a package is added/moved, the DevPilot integration changes, or a bug teaches a rule worth remembering. Not for ordinary features. Keep it under ~60 lines.
