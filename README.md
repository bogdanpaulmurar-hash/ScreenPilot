# ScreenPilot

Lightweight, enterprise-friendly screenshot, screen recording and
documentation platform for Windows — written in Python.

- **Documentation-first**: capture → annotate → step-by-step documents (roadmap).
- **Enterprise-safe**: no telemetry, no network access, minimal dependencies,
  no keyboard hooks, portable mode.
- **Bilingual**: English and Romanian from day one; new languages are a JSON file.
- **Automation-ready**: a headless core with a stable API for Autopilot.

> Status: **0.1.0 — Foundation (M0)**. Core engines and CLI are in place; the
> desktop UI arrives in M1. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Quick start (Windows, Python 3.10+)

```powershell
git clone https://github.com/bogdanpaulmurar-hash/ScreenPilot.git
cd ScreenPilot
python -m venv .venv
.venv\Scripts\activate
pip install -e .

python -m screenpilot monitors
python -m screenpilot capture region --rect 0,0,800,600
python -m screenpilot capture all --delay 3
python -m screenpilot --lang ro hotkeys
```

Captures go to `Pictures\ScreenPilot` and are copied to the clipboard
(configurable). Put an empty `portable.flag` file in the folder to keep all
data next to the app.

## Use from Python / Autopilot

```python
from screenpilot.app import create_app
from screenpilot.capture import Rect

app = create_app()
result = app.capture.capture_region(Rect(0, 0, 800, 600))
app.run_after_capture(result)  # the user's configured action chain
```

## Development

```powershell
pip install -e ".[dev]"
python -m pytest
```

Docs: [Architecture](docs/ARCHITECTURE.md) · [Roadmap](docs/ROADMAP.md) ·
[Dependency review](docs/DEPENDENCIES.md) · [Release notes](docs/releases/)

Commits, code and technical docs are in English; the UI is localized.
