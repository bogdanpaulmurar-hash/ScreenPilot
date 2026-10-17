# Dependency Review

Every third-party dependency must be reviewed here **before** it is added.
Criteria: maturity, maintenance, release cadence, license, reputation,
enterprise suitability, security history.

## Approved

| Package | Scope | License | Assessment |
|---|---|---|---|
| Pillow | core (save, convert, future editor ops) | MIT-CMU (HPND) | 15+ years, very active, quarterly releases, prompt CVE fixes, ubiquitous in enterprises. **Approved.** |
| PySide6-Essentials | optional `ui` extra | LGPL-3.0 | Official Qt for Python by The Qt Company, regular releases, widely deployed. Using the *Essentials* wheel avoids ~150 MB of unused Qt add-ons. LGPL obligations met by dynamic linking and shipping the license. **Approved for M1.** |
| pytest | dev only | MIT | Not shipped. **Approved.** |

## Evaluated and rejected / deferred

| Package | Reason |
|---|---|
| mss | Good library, but redundant: our ~250-line ctypes GDI backend covers it with zero dependencies. Rejected. |
| pywin32 | Large compiled package; we need only a few Win32 calls, done via ctypes. Rejected. |
| keyboard / pynput | Use global low-level keyboard hooks, which EDR/antivirus tools commonly flag as keylogger behaviour. Rejected in favour of `RegisterHotKey`. |
| pyautogui | Pulls several dependencies; not needed. Rejected. |
| opencv-python | ~90 MB; only needed for advanced UI-element detection. Deferred to M6 as an optional plugin. |
| pytesseract + Tesseract | Tesseract is mature (Apache-2.0) but adds ~40-80 MB with language data. Planned as *optional* OCR backend; default OCR will be the built-in Windows OCR engine (no install). Deferred to M3. |
| PyInstaller | **Approved, build-time only** (not imported by the app). GPL-2.0 with the bootloader exception, which explicitly allows distributing the generated executables under any licence. Mature (15+ years), active releases. Used by CI to produce the portable zip. Note: unsigned executables may trigger SmartScreen; code signing to be added with a corporate certificate. |

## Optional external tools (not bundled, not imported)

| Tool | Used for | Licence | Assessment |
|---|---|---|---|
| FFmpeg (`ffmpeg.exe`) | MP4 (H.264) encoding, audio from DirectShow devices, high-quality GIF conversion | LGPL-2.1+ or GPL-2.0+ depending on the build | The reference media toolkit for 20+ years, very active, used by Chrome, VLC, OBS, ShareX. **Run as a separate process** — ScreenPilot pipes raw frames to it and never links its code, so the build's licence does not extend to ScreenPilot. **Not bundled by default**: IT chooses and approves a build (e.g. `winget install Gyan.FFmpeg`, or an LGPL build from BtbN), then either puts it on `PATH`, drops `ffmpeg.exe` next to `ScreenPilot.exe`, or sets *Settings › Recording › FFmpeg path* (lockable by policy). Security notes: ffmpeg is only invoked with arguments built by ScreenPilot (no shell), never fed untrusted media during recording, and opens no network connections in these modes. Without it, ScreenPilot still records **GIF** with the built-in Pillow encoder. |

Rejected for recording:

| Package | Reason |
|---|---|
| imageio-ffmpeg / PyAV | Download or bundle FFmpeg binaries inside a Python wheel (opaque to IT review, ~60 MB). The external-process approach keeps the binary visible and replaceable. |
| sounddevice / PyAudio | Native PortAudio dependency just for microphone input; FFmpeg's DirectShow input covers it. |
| OBS / libobs | Excellent but huge and GPL; overkill for an embedded recorder. |
| Global mouse hooks for click effects | Flagged by EDR as keylogger-like. Clicks are polled with `GetAsyncKeyState` during recording only. |

## Policy

* No telemetry, analytics or crash-reporting SDKs.
* No dependency that opens network connections on its own.
* Pin minimum versions in `pyproject.toml`; lock exact versions for releases.
* Prefer Windows built-ins (GDI, Windows OCR, Media Foundation) over bundling.
