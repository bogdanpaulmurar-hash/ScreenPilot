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

## Policy

* No telemetry, analytics or crash-reporting SDKs.
* No dependency that opens network connections on its own.
* Pin minimum versions in `pyproject.toml`; lock exact versions for releases.
* Prefer Windows built-ins (GDI, Windows OCR, Media Foundation) over bundling.
