"""Command-line interface.

Useful today for scripted / automated captures (and for Autopilot), and as a
smoke test of the headless core on any Windows machine::

    python -m screenpilot monitors
    python -m screenpilot capture all --out C:/Temp/desk.png
    python -m screenpilot capture region --rect 0,0,800,600 --delay 3
    python -m screenpilot --lang ro hotkeys
    python -m screenpilot record --rect 0,0,1280,720 --seconds 10 --out C:/Temp/demo.mp4
    python -m screenpilot record-info
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
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
    gui = sub.add_parser("gui", help="start the desktop app (system tray)")
    gui.add_argument("--autostart", action="store_true", help="started with Windows: stay in the tray")
    setup = sub.add_parser("setup", help="Start-menu shortcut, autostart and icon for this installation")
    setup.add_argument("--start-menu", action="store_true", help="create the Start-menu shortcut")
    setup.add_argument("--autostart", choices=("on", "off"), help="start with Windows")
    setup.add_argument("--remove", action="store_true", help="remove the shortcut and the autostart entry")
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

    rec = sub.add_parser("record", help="record the screen to MP4 (needs ffmpeg) or GIF")
    rec.add_argument("--rect", help="x,y,width,height in virtual-desktop pixels (default: primary monitor)")
    rec.add_argument("--seconds", type=float, default=5.0, help="duration (Ctrl+C stops earlier)")
    rec.add_argument("--format", choices=("mp4", "gif"), help="default: the recording.format setting")
    rec.add_argument("--fps", type=int, help="frame rate")
    rec.add_argument("--out", type=Path, help="output file (default: the recordings folder)")
    rec.add_argument("--no-effects", action="store_true", help="no cursor highlight / click ripples")
    sub.add_parser("record-info", help="show ffmpeg, encoder and audio device detection")
    return parser


def _record(app, args) -> int:
    t = app.translator.t
    service = app.recording
    opts = service.options()
    fmt = args.format or (args.out.suffix.lstrip(".").lower() if args.out and args.out.suffix else opts.format)
    changes = {"format": fmt, "countdown": 0}
    if args.fps:
        changes["fps" if fmt == "mp4" else "gif_fps"] = args.fps
    if args.no_effects:
        changes.update(highlight_cursor=False, click_effects=False)
    opts = dataclasses.replace(opts, **changes)
    if args.rect:
        rect = Rect.parse(args.rect)
    else:
        primary = next((m for m in app.capture.monitors() if m.primary), app.capture.monitors()[0])
        rect = primary.bounds
    recorder = service.start(rect, opts, output=args.out)
    print(t("cli.record.started", seconds=args.seconds, encoder=recorder.encoder))
    try:
        end = time.monotonic() + args.seconds
        while time.monotonic() < end and recorder.active:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    result = recorder.stop(wait=True, timeout=120)
    if result is None:
        reason = recorder.error.message_key if recorder.error else "recording.error.generic"
        print(t("cli.capture.failed", reason=t(reason)) + (f" ({recorder.error})" if recorder.error else ""), file=sys.stderr)
        return 1
    print(
        t(
            "cli.record.saved",
            path=result.path,
            seconds=f"{result.duration:.1f}",
            fps=f"{result.capture_fps:.1f}",
            frames=result.frames_captured,
            grab=f"{result.avg_grab_ms:.1f}",
        )
    )
    return 0


def _setup(app, args) -> int:
    from screenpilot.branding import write_ico
    from screenpilot.platform.autostart import SHORTCUT_NAME, Autostart, create_shortcut, start_menu_dir

    autostart = Autostart()
    link = start_menu_dir() / SHORTCUT_NAME
    if args.remove:
        link.unlink(missing_ok=True)
        autostart.set_enabled(False)
        app.settings.set("general.start_with_windows", False)
        print(f"Removed {link} and the autostart entry")
        return 0
    icon = write_ico(app.paths.data_dir / "screenpilot.ico")
    print(f"Icon: {icon}")
    if args.start_menu:
        print(f"Shortcut: {create_shortcut(link, icon)}")
    if args.autostart:
        enabled = args.autostart == "on"
        app.settings.set("general.start_with_windows", enabled)
        autostart.set_enabled(enabled)
        print(f"Start with Windows: {'on' if enabled else 'off'} ({autostart.command})")
    return 0


def _record_info(app) -> None:
    t = app.translator.t
    info = app.recording.ffmpeg(refresh=True)
    if info is None:
        print(t("settings.rec.ffmpeg.missing"))
        return
    print(t("settings.rec.ffmpeg.found", version=info.version, path=info.path))
    h264 = sorted(e for e in info.encoders if "264" in e)
    print(f"  H.264 encoders listed: {', '.join(h264) or '-'}")
    try:
        print(f"  Encoder chosen (auto): {info.choose_encoder('auto')}")
    except ScreenPilotError as exc:
        print(f"  {t(exc.message_key)}")
    devices = info.audio_devices()
    print(f"  {t('settings.rec.devices')}: {', '.join(devices) if devices else '-'}")


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

        return gui_main([sys.argv[0]] + (["--autostart"] if args.autostart else []))

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
        elif args.command == "setup":
            return _setup(app, args)
        elif args.command == "record":
            return _record(app, args)
        elif args.command == "record-info":
            _record_info(app)
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
