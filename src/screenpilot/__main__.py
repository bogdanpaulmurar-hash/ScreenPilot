"""Command-line interface.

Useful today for scripted / automated captures (and for Autopilot), and as a
smoke test of the headless core on any Windows machine::

    python -m screenpilot monitors
    python -m screenpilot capture all --out C:/Temp/desk.png
    python -m screenpilot capture region --rect 0,0,800,600 --delay 3
    python -m screenpilot --lang ro hotkeys
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from screenpilot import __version__
from screenpilot.capture import CaptureResult, Rect
from screenpilot.core.errors import ScreenPilotError

CAPTURE_MODES = ("region", "window", "monitor", "current", "all")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screenpilot", description="ScreenPilot command line")
    parser.add_argument("--lang", help="UI language code (en, ro)")
    parser.add_argument("--verbose", action="store_true", help="also log to the console")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version")
    sub.add_parser("gui", help="start the desktop app (system tray)")
    sub.add_parser("monitors")
    sub.add_parser("windows")
    sub.add_parser("hotkeys")
    diag = sub.add_parser("diagnostics", help="export a diagnostic .zip (logs, timings, settings)")
    diag.add_argument("--out", type=Path, default=Path("screenpilot-diagnostics.zip"))

    cap = sub.add_parser("capture")
    cap.add_argument("mode", choices=CAPTURE_MODES)
    cap.add_argument("--rect", help="x,y,width,height in virtual-desktop pixels (region mode)")
    cap.add_argument("--index", type=int, default=1, help="monitor number (monitor mode)")
    cap.add_argument("--delay", type=int, default=0, help="seconds to wait before capturing")
    cap.add_argument("--out", type=Path, help="save to this file and skip the configured actions")
    return parser


def _do_capture(app, args) -> CaptureResult | None:
    service = app.capture
    if args.mode == "region":
        if not args.rect:
            raise ScreenPilotError("--rect is required for region mode")
        action = lambda: service.capture_region(Rect.parse(args.rect))  # noqa: E731
    elif args.mode == "window":
        action = service.capture_window
    elif args.mode == "monitor":
        action = lambda: service.capture_monitor(args.index)  # noqa: E731
    elif args.mode == "current":
        action = service.capture_current_monitor
    else:
        action = service.capture_all_monitors

    if args.delay:
        sub = app.bus.subscribe(
            "capture.countdown", lambda e: print(app.translator.t("capture.delay.countdown", **e.payload))
        )
        try:
            return service.capture_delayed(args.delay, action)
        finally:
            sub.cancel()
    return action()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(f"ScreenPilot {__version__}")
        return 0
    if args.command == "gui":
        from screenpilot.ui.main import main as gui_main

        return gui_main([sys.argv[0]])

    from screenpilot.app import create_app

    try:
        app = create_app(language=args.lang, console_log=args.verbose)
    except ScreenPilotError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    t = app.translator.t

    try:
        if args.command == "monitors":
            print(t("cli.monitors.header"))
            for m in app.capture.monitors():
                print(
                    "  "
                    + t(
                        "capture.monitor.label",
                        index=m.index,
                        width=m.bounds.width,
                        height=m.bounds.height,
                        x=m.bounds.x,
                        y=m.bounds.y,
                        primary=t("capture.monitor.primary") if m.primary else "",
                    )
                    + f"  ({int(m.scale * 100)} %)"
                )
        elif args.command == "windows":
            for w in app.capture.windows():
                print(f"  {w.handle:>10}  {w.bounds}  {w.title}")
        elif args.command == "hotkeys":
            print(t("cli.hotkeys.header"))
            for action, hotkey in app.hotkeys.bindings().items():
                print("  " + t("cli.hotkeys.row", hotkey=hotkey or "-", action=t(f"action.{action}")))
        elif args.command == "diagnostics":
            app.capture.freeze()  # one measured capture so the bundle has timings
            print(app.export_diagnostics(args.out))
        elif args.command == "capture":
            result = _do_capture(app, args)
            if result is None:
                print(t("capture.delay.cancelled"))
                return 1
            if args.out:
                path = result.image.save(args.out, image_format=args.out.suffix.lstrip(".") or "png")
            else:
                steps, ctx = app.run_after_capture(result)
                for step in steps:
                    key = f"workflow.result.{step.status.value}"
                    print("  " + t(key, action=t(f"workflow.action.{step.action_id}"), reason=step.message))
                path = ctx.saved_path
            if path is not None:
                print(t("cli.capture.saved", path=path, width=result.image.width, height=result.image.height))
        return 0
    except ScreenPilotError as exc:
        print(t("cli.capture.failed", reason=t(exc.message_key)) + f" ({exc})", file=sys.stderr)
        return 1
    finally:
        app.shutdown()


if __name__ == "__main__":
    sys.exit(main())
