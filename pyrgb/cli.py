"""Command line interface for py_rgb."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

from . import __version__
from .backends import BACKEND_NAMES, BackendError, create_backend
from .color import RGB
from .config import (
    coerce_like_default,
    config_path,
    default_config,
    get_path,
    load_config,
    save_config,
    set_path,
)
from .effects import REGISTRY, create_effect, describe, effect_names
from .engine import Engine

log = logging.getLogger("pyrgb")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pyrgb",
        description="Control motherboard / system RGB from the CLI or a Qt6 GUI.",
    )
    p.add_argument("--version", action="version", version=f"py_rgb {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument(
        "--backend",
        choices=BACKEND_NAMES,
        default=None,
        help="override the configured backend (default from config.toml)",
    )
    p.add_argument("--config", default=None, help="path to a config file (default: ./config.toml)")

    # the same flags are accepted after the subcommand as well
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--backend", choices=BACKEND_NAMES, default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    common.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser(
        "gui", parents=[common], help="launch the Qt6 interface (default when run bare)"
    )
    sp.add_argument("--minimized", action="store_true", help="start hidden in the tray")

    sp = sub.add_parser(
        "tray",
        parents=[common],
        help="start the daemon (if needed) and sit in the tray - used by autostart",
    )
    sp.add_argument(
        "--no-daemon", action="store_true", help="do not start a daemon, run the engine in-process"
    )
    sp.add_argument(
        "--window", action="store_true", help="also show the window instead of only the tray icon"
    )

    sub.add_parser("devices", parents=[common], help="list detected RGB devices")
    sub.add_parser("doctor", parents=[common], help="diagnose why hardware is not detected")
    sub.add_parser("effects", parents=[common], help="list available effects and their options")

    sp = sub.add_parser("audio-devices", parents=[common], help="list audio devices usable for the audio effect")
    sp.add_argument("--all", action="store_true", help="include devices without output channels")

    sp = sub.add_parser("set", parents=[common], help="apply a single static color and exit")
    sp.add_argument("color", help="e.g. '#ff8800', 'ff8800', '255,136,0' or 'orange'")
    sp.add_argument("-b", "--brightness", type=float, help="0..1 global brightness")
    sp.add_argument("-d", "--device", action="append", help="device index or name (repeatable)")

    sp = sub.add_parser("off", parents=[common], help="turn all LEDs off")
    sp.add_argument("-d", "--device", action="append", help="device index or name (repeatable)")

    sp = sub.add_parser("run", parents=[common], help="run an animated effect until Ctrl+C")
    sp.add_argument("effect", nargs="?", choices=effect_names(), help="effect name")
    sp.add_argument("-c", "--color", help="primary color override")
    sp.add_argument("-s", "--speed", type=float, help="speed override")
    sp.add_argument("-b", "--brightness", type=float, help="0..1 global brightness")
    sp.add_argument("--fps", type=int, help="frame rate override")
    sp.add_argument("-d", "--device", action="append", help="device index or name (repeatable)")
    sp.add_argument("--duration", type=float, help="stop after N seconds")
    sp.add_argument("--preview", action="store_true", help="print the live color to stdout")
    sp.add_argument(
        "-o", "--option", action="append", metavar="KEY=VALUE",
        help="effect-specific option (repeatable), e.g. -o pulse=true",
    )

    sp = sub.add_parser("daemon", parents=[common], help="run the headless daemon (foreground)")
    sp.add_argument("--log", help="write logs to this file instead of the console")

    sp = sub.add_parser(
        "service", parents=[common], help="install/manage autostart for the daemon"
    )
    sp.add_argument(
        "action",
        choices=["install", "uninstall", "start", "stop", "restart", "status"],
    )
    sp.add_argument(
        "--mode",
        choices=["startup", "task", "service"],
        default="startup",
        help="startup = HKCU Run key at logon (default, no admin, keeps audio); "
        "task = Scheduled Task (needs admin); "
        "service = real Windows service (admin, session 0, no audio)",
    )
    sp.add_argument(
        "--tray",
        action="store_true",
        help="start the tray icon together with the daemon (startup mode only)",
    )

    sp = sub.add_parser("ctl", parents=[common], help="send a command to a running daemon")
    sp.add_argument(
        "action",
        choices=["status", "reload", "effect", "color", "brightness", "devices", "off", "on", "stop"],
    )
    sp.add_argument("value", nargs="?", help="effect name / colour / brightness value")
    sp.add_argument(
        "-o", "--option", action="append", metavar="KEY=VALUE", help="effect option (repeatable)"
    )

    cfg_p = sub.add_parser("config", parents=[common], help="inspect or edit the local config file")
    cfg_sub = cfg_p.add_subparsers(dest="config_command", required=True)
    cfg_sub.add_parser("path", help="print the config file path")
    cfg_sub.add_parser("show", help="print the effective configuration")
    cfg_sub.add_parser("init", help="write a default config.toml in the root folder")
    g = cfg_sub.add_parser("get", help="read one value, e.g. general.fps")
    g.add_argument("key")
    s = cfg_sub.add_parser("set", help="write one value, e.g. general.fps 60")
    s.add_argument("key")
    s.add_argument("value")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s" if args.verbose else "%(message)s",
    )

    path = None
    if args.config:
        from pathlib import Path

        path = Path(args.config).expanduser().resolve()
    cfg = load_config(path)

    command = args.command or "gui"
    try:
        if command == "gui":
            return cmd_gui(args, cfg)
        if command == "tray":
            return cmd_tray(args, cfg, path)
        if command == "devices":
            return cmd_devices(args, cfg)
        if command == "doctor":
            return cmd_doctor(args, cfg)
        if command == "effects":
            return cmd_effects(args, cfg)
        if command == "audio-devices":
            return cmd_audio_devices(args, cfg)
        if command == "set":
            return cmd_set(args, cfg)
        if command == "off":
            return cmd_off(args, cfg)
        if command == "run":
            return cmd_run(args, cfg)
        if command == "daemon":
            return cmd_daemon(args, cfg, path)
        if command == "service":
            return cmd_service(args, cfg, path)
        if command == "ctl":
            return cmd_ctl(args, cfg)
        if command == "config":
            return cmd_config(args, cfg, path)
    except BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    parser.print_help()
    return 1


# ---------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------

def cmd_gui(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    try:
        from .gui import run_gui
    except ImportError as exc:
        print(
            f"error: Qt6 GUI unavailable ({exc}); install PyQt6 or use 'pyrgb run'",
            file=sys.stderr,
        )
        return 2
    if getattr(args, "minimized", False):
        cfg.setdefault("gui", {})["start_minimized"] = True
    return run_gui(cfg, backend_name=args.backend)


def cmd_tray(args: argparse.Namespace, cfg: dict[str, Any], path: Any) -> int:
    """Autostart entry point: ensure a daemon is up, then show the tray icon."""
    from . import service as svc

    try:
        from .gui import run_gui
    except ImportError as exc:
        print(f"error: Qt6 GUI unavailable ({exc})", file=sys.stderr)
        return 2

    # windowed builds have no console: always leave a trace on disk
    target_cfg = path or config_path()
    _add_file_log(target_cfg.parent / "pyrgb-tray.log")
    log.info("tray startup: config=%s frozen=%s", target_cfg, getattr(sys, "frozen", False))

    gui_cfg = cfg.setdefault("gui", {})
    gui_cfg["tray"] = True
    gui_cfg.setdefault("close_to_tray", True)
    if not args.window:
        gui_cfg["start_minimized"] = True

    if not args.no_daemon:
        daemon_cfg = cfg.get("daemon", {})
        ok, message = svc.daemon_spawn(
            path or config_path(),
            wait=15.0,
            host=daemon_cfg.get("host", "127.0.0.1"),
            port=int(daemon_cfg.get("port", 6743)),
        )
        log.info("daemon: %s", message)
        if not ok:
            # fall through anyway: the GUI will drive the hardware locally
            log.warning("continuing without a daemon (%s)", message)

    try:
        return run_gui(cfg, backend_name=args.backend)
    except Exception:
        log.exception("tray mode crashed")
        raise


def _add_file_log(path: Any) -> None:
    try:
        handler = logging.FileHandler(str(path), encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)


def cmd_devices(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    backend = create_backend(cfg, args.backend)
    try:
        infos = backend.devices()
        print(f"backend: {backend.name}")
        if not infos:
            print("no devices detected")
            return 1
        for info in infos:
            zones = f" zones: {', '.join(info.zones)}" if info.zones else ""
            print(f"  [{info.index}] {info.name}  ({info.kind}, {info.led_count} LEDs){zones}")
        return 0
    finally:
        backend.close()


def cmd_doctor(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    """Check every hardware path and explain what is missing."""
    import socket

    from .backends.msi_mystic import MSI_PID, MSI_VID, MSIMysticBackend

    ok = True
    print("py_rgb doctor\n")

    # --- MSI Mystic Light ------------------------------------------------
    print("MSI Mystic Light (native HID)")
    try:
        import hid  # noqa: F401

        entry = MSIMysticBackend.find()
        if entry is None:
            print(f"  x  no {MSI_VID:04x}:{MSI_PID:04x} controller present")
        else:
            print(f"  ok {entry.get('manufacturer_string')} {entry.get('product_string')}")
            backend = MSIMysticBackend()
            try:
                backend.connect()
                print("  ok HID device opens")
                state = backend.read_state()
                if state is None:
                    print("  !  controller state not readable (writes may still work)")
                else:
                    mode, rgb = state["mode"], state["color"]
                    print(
                        f"  ok controller responds: mode={mode} ({state['mode_name']}), "
                        f"color={rgb.to_hex()}, brightness={state['brightness']}/5"
                    )
            except BackendError as exc:
                ok = False
                print(f"  x  {exc}")
            finally:
                backend.close()
    except ImportError:
        print("  x  hidapi not installed  ->  pip install hidapi")

    # --- OpenRGB ---------------------------------------------------------
    orgb = cfg.get("openrgb", {})
    host, port = orgb.get("host", "127.0.0.1"), int(orgb.get("port", 6742))
    print(f"\nOpenRGB SDK server ({host}:{port})")
    with socket.socket() as s:
        s.settimeout(1.0)
        reachable = s.connect_ex((host, port)) == 0
    if reachable:
        print("  ok server reachable")
        try:
            backend = create_backend(cfg, "openrgb")
            devs = backend.devices()
            print(f"  ok {len(devs)} device(s)" if devs else "  !  connected but 0 devices detected")
            for info in devs:
                print(f"       [{info.index}] {info.name}")
            if not devs:
                print("       run OpenRGB as Administrator and stop vendor RGB services")
            backend.close()
        except BackendError as exc:
            ok = False
            print(f"  x  {exc}")
    else:
        print("  x  not reachable -> start OpenRGB and enable the SDK server")

    # --- signal sources --------------------------------------------------
    print("\nEffect signal sources")
    try:
        import psutil  # noqa: F401

        print("  ok psutil (cpu load)")
    except ImportError:
        print("  x  psutil missing -> pip install psutil")

    from .sources import CPUTempSource

    temp = CPUTempSource()
    temp.start()
    if temp.available:
        print(f"  ok cpu temperature: {temp.value:.0f}\u00b0C via {temp.provider}")
    else:
        print(
            "  !  no cpu temperature source (run MSI Afterburner or "
            "LibreHardwareMonitor, or use effects.cpu.source = load)"
        )
    temp.stop()
    from .sources import AudioSource

    audio = AudioSource()
    audio.start()
    time.sleep(0.4)
    if audio.available:
        print(f"  ok audio capture: {audio.mode} - {audio.source_name}")
    else:
        print(f"  x  audio capture unavailable: {audio.error}")
    audio.stop()

    try:
        from PyQt6.QtWidgets import QApplication  # noqa: F401

        print("  ok PyQt6 (gui)")
    except ImportError:
        print("  x  PyQt6 missing -> pip install PyQt6")

    print(f"\nconfig: {config_path()}")
    return 0 if ok else 1


def cmd_effects(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    print(describe())
    return 0


def cmd_audio_devices(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from .sources import list_audio_devices

    from .sources import list_loopback_speakers

    speakers = list_loopback_speakers()
    if speakers:
        print("WASAPI loopback speakers (best for the 'audio' effect):")
        for name in speakers:
            print(f"  - {name}")
        print()

    devices = list_audio_devices()
    if not devices:
        print("no audio devices found (pip install sounddevice)")
        return 0 if speakers else 1
    print("PortAudio devices:")
    for dev in devices:
        if not args.all and dev["outputs"] == 0 and dev["inputs"] == 0:
            continue
        print(
            f"  [{dev['index']:>2}] {dev['name']}  "
            f"({dev['hostapi']}, in={dev['inputs']}, out={dev['outputs']})"
        )
    print(
        "\nSet audio.device in config.toml to a speaker name (soundcard loopback) "
        "or a PortAudio index; leave blank for auto-detection."
    )
    return 0


def cmd_set(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    color = RGB.parse(args.color)
    _apply_device_filter(cfg, args.device)
    if args.brightness is not None:
        cfg["general"]["brightness"] = args.brightness
    backend = create_backend(cfg, args.backend)
    try:
        engine = Engine(backend, cfg, effect=create_effect("static", cfg, color=color.to_hex()))
        engine.apply_once()
        print(f"{backend.name}: set {color.to_hex()} on device(s) {engine.targets}")
        return 0
    finally:
        backend.close()


def cmd_off(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    _apply_device_filter(cfg, args.device)
    backend = create_backend(cfg, args.backend)
    try:
        engine = Engine(backend, cfg, effect=create_effect("off", cfg))
        engine.apply_once()
        print(f"{backend.name}: LEDs off on device(s) {engine.targets}")
        return 0
    finally:
        backend.close()


def cmd_run(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    _apply_device_filter(cfg, args.device)
    if args.fps:
        cfg["general"]["fps"] = args.fps
    if args.brightness is not None:
        cfg["general"]["brightness"] = args.brightness

    name = args.effect or cfg["general"].get("effect", "breathing")
    overrides: dict[str, Any] = {}
    if args.color:
        overrides["color"] = RGB.parse(args.color).to_hex()
    if args.speed is not None:
        overrides["speed"] = args.speed
    for item in args.option or []:
        if "=" not in item:
            print(f"error: --option expects KEY=VALUE (got {item!r})", file=sys.stderr)
            return 1
        key, value = item.split("=", 1)
        overrides[key.strip()] = value.strip()

    cls = REGISTRY[name]
    overrides = {k: v for k, v in overrides.items() if k in cls.options or k not in ("color", "speed")}
    effect = create_effect(name, cfg, **overrides)

    backend = create_backend(cfg, args.backend)
    engine = Engine(backend, cfg, effect=effect)
    if args.preview:
        engine.frame_hook = _print_preview

    print(
        f"{backend.name}: running '{name}' at {engine.fps} fps on device(s) {engine.targets} "
        f"- Ctrl+C to stop"
    )
    engine.start()
    deadline = time.monotonic() + args.duration if args.duration else None
    try:
        while engine.running:
            if deadline and time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        print()
    finally:
        engine.stop(clear=False)
        if args.preview:
            sys.stdout.write("\n")
        backend.close()
    if engine.error:
        print(f"error: {engine.error}", file=sys.stderr)
        return 2
    return 0


def cmd_daemon(args: argparse.Namespace, cfg: dict[str, Any], path: Any) -> int:
    from .daemon import run_daemon

    if args.log:
        handler = logging.FileHandler(args.log, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.handlers[:] = [handler]
        root.setLevel(logging.INFO)
    else:
        print("py_rgb daemon - Ctrl+C to stop")
    return run_daemon(path, args.backend)


def cmd_service(args: argparse.Namespace, cfg: dict[str, Any], path: Any) -> int:
    from . import service as svc

    target = path or config_path()
    with_tray = bool(getattr(args, "tray", False))
    if args.mode == "startup":
        actions = {
            "install": lambda: svc.startup_install(target, with_tray),
            "uninstall": lambda: svc.startup_uninstall(),
            "start": lambda: svc.daemon_spawn(target),
            "stop": lambda: svc.daemon_kill(),
            "restart": lambda: (svc.daemon_kill(), time.sleep(1), svc.daemon_spawn(target))[2],
            "status": lambda: _startup_status(svc, cfg),
        }
        ok, out = actions[args.action]()
    elif args.mode == "task":
        actions = {
            "install": lambda: svc.task_install(target),
            "uninstall": lambda: svc.task_uninstall(),
            "start": lambda: svc.task_start(),
            "stop": lambda: svc.task_stop(),
            "restart": lambda: (svc.task_stop(), svc.task_start())[1],
            "status": lambda: (
                svc.task_installed(),
                f"scheduled task '{svc.TASK_NAME}' "
                + ("installed" if svc.task_installed() else "not installed"),
            ),
        }
        ok, out = actions[args.action]()
    else:
        action = "remove" if args.action == "uninstall" else args.action
        ok, out = svc.service_control(action, target)

    if not ok and "Access is denied" in out:
        out += (
            "\nhint: --mode task/service needs an elevated prompt; "
            "use --mode startup for a no-admin logon entry"
        )
    print(out.strip() or ("ok" if ok else "failed"))
    if args.action == "install" and ok:
        launch = (
            svc.startup_command(target, with_tray)
            if args.mode == "startup"
            else svc.daemon_command(target)
        )
        print(f"launches: {' '.join(launch)}")
        if args.mode == "service":
            print("note: a session-0 service cannot capture audio; use --mode task for that")
    return 0 if ok else 1


def _startup_status(svc: Any, cfg: dict[str, Any]) -> tuple[bool, str]:
    from .ipc import daemon_status

    installed = svc.startup_installed()
    daemon_cfg = cfg.get("daemon", {})
    status = daemon_status(
        daemon_cfg.get("host", "127.0.0.1"),
        int(daemon_cfg.get("port", 6743)),
        str(daemon_cfg.get("token", "")),
    )
    lines = [
        f"logon entry : {'installed' if installed else 'not installed'}",
    ]
    if installed:
        lines.append(f"              {'daemon + tray' if svc.startup_has_tray() else 'daemon only'}")
        lines.append(f"              {svc.startup_entry()}")
    lines.append(f"daemon      : {'running' if status else 'not running'}")
    if status:
        lines.append(
            f"              backend={status.get('backend')} effect={status.get('effect')} "
            f"fps={status.get('fps')} uptime={status.get('uptime')}s"
        )
    return True, "\n".join(lines)


def cmd_ctl(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    import json

    from .ipc import send_command

    daemon_cfg = cfg.get("daemon", {})
    kw: dict[str, Any] = {
        "host": daemon_cfg.get("host", "127.0.0.1"),
        "port": int(daemon_cfg.get("port", 6743)),
        "token": str(daemon_cfg.get("token", "")),
    }

    action = args.action
    payload: dict[str, Any] = {}
    if action == "stop":
        action = "shutdown"
    elif action == "effect":
        if not args.value:
            print("error: 'ctl effect' needs an effect name", file=sys.stderr)
            return 1
        payload["name"] = args.value
        params: dict[str, Any] = {}
        for item in args.option or []:
            if "=" not in item:
                print(f"error: --option expects KEY=VALUE (got {item!r})", file=sys.stderr)
                return 1
            key, value = item.split("=", 1)
            params[key.strip()] = value.strip()
        payload["params"] = params
    elif action in ("color", "brightness"):
        if not args.value:
            print(f"error: 'ctl {action}' needs a value", file=sys.stderr)
            return 1
        payload["value"] = args.value

    try:
        reply = send_command(action, **kw, **payload)
    except ConnectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("start one with 'pyrgb daemon' or 'pyrgb service install'", file=sys.stderr)
        return 2

    if not reply.get("ok"):
        print(f"error: {reply.get('error', 'command failed')}", file=sys.stderr)
        return 1
    reply.pop("ok", None)
    print(json.dumps(reply, indent=2) if reply else "ok")
    return 0


def cmd_config(args: argparse.Namespace, cfg: dict[str, Any], path: Any) -> int:
    from .config import dumps_toml

    target = path or config_path()
    if args.config_command == "path":
        print(target)
        return 0
    if args.config_command == "show":
        print(dumps_toml(cfg))
        return 0
    if args.config_command == "init":
        written = save_config(default_config(), target)
        print(f"wrote {written}")
        return 0
    if args.config_command == "get":
        value = get_path(cfg, args.key, "<unset>")
        print(value)
        return 0
    if args.config_command == "set":
        value = coerce_like_default(args.key, args.value)
        set_path(cfg, args.key, value)
        written = save_config(cfg, target)
        print(f"{args.key} = {value!r}  ->  {written}")
        return 0
    return 1


# ---------------------------------------------------------------------

def _apply_device_filter(cfg: dict[str, Any], devices: list[str] | None) -> None:
    if devices:
        cfg.setdefault("general", {})["devices"] = list(devices)


_SPINNER = "\u2588" * 3


def _print_preview(colors: list[RGB]) -> None:
    c = colors[0]
    sys.stdout.write(f"\r\x1b[38;2;{c.r};{c.g};{c.b}m{_SPINNER}\x1b[0m {c.to_hex()}   ")
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
