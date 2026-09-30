"""Headless py_rgb daemon: owns the hardware, driven by config.toml + commands.

The daemon is the single process that talks to the LEDs.  The Qt6 app never
opens the hardware while a daemon is running: it edits ``config.toml`` and tells
the daemon to re-read it, or sends live commands over the control channel.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .backends import RGBBackend, create_backend
from .color import RGB
from .config import config_path, load_config
from .effects import REGISTRY, create_effect
from .engine import Engine
from .ipc import DEFAULT_HOST, DEFAULT_PORT, ControlServer

log = logging.getLogger(__name__)

#: commands the control channel understands
COMMANDS = (
    "ping",
    "status",
    "reload",
    "effect",
    "color",
    "brightness",
    "devices",
    "off",
    "on",
    "shutdown",
)


class Daemon:
    """Runs the effect engine and serves control commands."""

    def __init__(self, path: Path | None = None, backend_name: str | None = None) -> None:
        self.path = path or config_path()
        self.backend_name = backend_name
        self.cfg: dict[str, Any] = {}
        self.backend: RGBBackend | None = None
        self.engine: Engine | None = None
        self.server: ControlServer | None = None
        self.started_at = 0.0
        self.paused = False
        self._lock = threading.RLock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self.cfg = load_config(self.path)
        self._open_hardware()

        daemon_cfg = self.cfg.get("daemon", {})
        self.server = ControlServer(
            self._handle,
            host=daemon_cfg.get("host", DEFAULT_HOST),
            port=int(daemon_cfg.get("port", DEFAULT_PORT)),
            token=str(daemon_cfg.get("token", "")),
        )
        self.server.start()
        self.started_at = time.time()

        assert self.engine is not None
        self.engine.start()
        log.info(
            "daemon up: backend=%s effect=%s devices=%s",
            self.backend.name if self.backend else "?",
            self.engine.effect.name,
            self.engine.targets,
        )

    def _open_hardware(self) -> None:
        backend = create_backend(self.cfg, self.backend_name)
        engine = Engine(backend, self.cfg)
        self.backend, self.engine = backend, engine

    def run_forever(self, poll: float = 0.5) -> None:
        """Block until ``stop()`` is called (or the config file changes)."""
        watch = bool(self.cfg.get("daemon", {}).get("watch_config", True))
        last_mtime = _mtime(self.path)
        while not self._stop.is_set():
            self._stop.wait(poll)
            if self._stop.is_set():
                break
            if watch:
                mtime = _mtime(self.path)
                if mtime and mtime != last_mtime:
                    last_mtime = mtime
                    log.info("config file changed on disk; reloading")
                    try:
                        self.reload()
                    except Exception as exc:  # noqa: BLE001
                        log.error("reload failed: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        if self.server is not None:
            self.server.stop()
            self.server = None
        if self.engine is not None:
            clear = bool(self.cfg.get("daemon", {}).get("clear_on_exit", False))
            self.engine.stop(clear=clear)
            self.engine = None
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:  # pragma: no cover
                pass
            self.backend = None
        log.info("daemon stopped")

    # ------------------------------------------------------------------
    # config reload
    # ------------------------------------------------------------------
    def reload(self) -> dict[str, Any]:
        """Re-read config.toml and apply it without dropping the hardware."""
        with self._lock:
            new_cfg = load_config(self.path)
            old_backend = (self.cfg.get("general", {}) or {}).get("backend")
            new_backend = (new_cfg.get("general", {}) or {}).get("backend")
            hardware_changed = (
                old_backend != new_backend
                or self.cfg.get("openrgb") != new_cfg.get("openrgb")
                or self.cfg.get("msi") != new_cfg.get("msi")
            )
            self.cfg = new_cfg

            if hardware_changed or self.engine is None or self.backend is None:
                log.info("hardware settings changed; re-opening backend")
                if self.engine is not None:
                    self.engine.stop()
                if self.backend is not None:
                    try:
                        self.backend.close()
                    except Exception:  # pragma: no cover
                        pass
                self._open_hardware()
                assert self.engine is not None
                if not self.paused:
                    self.engine.start()
                return self.status()

            general = new_cfg.get("general", {})
            engine = self.engine
            engine.cfg = new_cfg
            engine.fps = max(1, int(general.get("fps", 30)))
            engine.set_brightness(float(general.get("brightness", 1.0)))
            engine.set_devices(engine._resolve_targets(general.get("devices") or []))
            engine.set_effect(create_effect(general.get("effect", "breathing"), new_cfg))
            if not self.paused and not engine.running:
                engine.start()
            return self.status()

    # ------------------------------------------------------------------
    # command handling
    # ------------------------------------------------------------------
    def _handle(self, message: dict[str, Any]) -> dict[str, Any]:
        cmd = str(message.get("cmd", "")).lower()
        if cmd not in COMMANDS:
            return {"ok": False, "error": f"unknown command {cmd!r}", "commands": list(COMMANDS)}
        try:
            return getattr(self, f"_cmd_{cmd}")(message)
        except Exception as exc:  # noqa: BLE001
            log.exception("command %s failed", cmd)
            return {"ok": False, "error": str(exc)}

    def _cmd_ping(self, _message: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "pong": True}

    def _cmd_status(self, _message: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, **self.status()}

    def _cmd_reload(self, _message: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, **self.reload()}

    def _cmd_effect(self, message: dict[str, Any]) -> dict[str, Any]:
        name = str(message.get("name", "")).lower()
        if name not in REGISTRY:
            return {"ok": False, "error": f"unknown effect {name!r}", "effects": list(REGISTRY)}
        params = dict(message.get("params") or {})
        with self._lock:
            engine = self._require_engine()
            engine.set_effect(create_effect(name, self.cfg, **params))
            self.paused = False
            if not engine.running:
                engine.start()
        return {"ok": True, **self.status()}

    def _cmd_color(self, message: dict[str, Any]) -> dict[str, Any]:
        color = RGB.parse(message.get("value", "#ffffff")).to_hex()
        with self._lock:
            engine = self._require_engine()
            effect = engine.effect
            if "color" in getattr(type(effect), "options", {}):
                engine.set_param("color", color)
            else:
                engine.set_effect(create_effect("static", self.cfg, color=color))
            self.paused = False
            if not engine.running:
                engine.start()
        return {"ok": True, "color": color}

    def _cmd_brightness(self, message: dict[str, Any]) -> dict[str, Any]:
        value = float(message.get("value", 1.0))
        self._require_engine().set_brightness(value)
        return {"ok": True, "brightness": round(value, 3)}

    def _cmd_devices(self, _message: dict[str, Any]) -> dict[str, Any]:
        backend = self._require_backend()
        return {
            "ok": True,
            "backend": backend.name,
            "devices": [
                {
                    "index": d.index,
                    "name": d.name,
                    "leds": d.led_count,
                    "kind": d.kind,
                    "zones": d.zones,
                }
                for d in backend.devices()
            ],
        }

    def _cmd_off(self, _message: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            engine = self._require_engine()
            engine.stop()
            engine.apply_once(create_effect("off", self.cfg))
            self.paused = True
        return {"ok": True, "paused": True}

    def _cmd_on(self, _message: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            engine = self._require_engine()
            self.paused = False
            if not engine.running:
                engine.start()
        return {"ok": True, "paused": False}

    def _cmd_shutdown(self, _message: dict[str, Any]) -> dict[str, Any]:
        threading.Timer(0.2, self.stop).start()
        return {"ok": True, "stopping": True}

    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        engine, backend = self.engine, self.backend
        effect = engine.effect if engine else None
        return {
            "backend": backend.name if backend else "none",
            "running": bool(engine and engine.running),
            "paused": self.paused,
            "effect": effect.name if effect else None,
            "params": dict(effect.params) if effect else {},
            "brightness": round(engine.brightness, 3) if engine else 0.0,
            "fps": engine.fps if engine else 0,
            "devices": engine.targets if engine else [],
            "device_names": [d.name for d in backend.devices()] if backend else [],
            "frames": engine.frames_rendered if engine else 0,
            "color": engine.last_color.to_hex() if engine and engine.last_color else "#000000",
            "effects_available": list(REGISTRY),
            "all_devices": (
                [
                    {"index": d.index, "name": d.name, "leds": d.led_count, "kind": d.kind}
                    for d in backend.devices()
                ]
                if backend
                else []
            ),
            "cpu": round(engine.cpu.value, 3) if engine else 0.0,
            "audio": round(engine.audio.level, 3) if engine else 0.0,
            "audio_mode": engine.audio.mode if engine else "none",
            "error": engine.error if engine else None,
            "uptime": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
            "config": str(self.path),
        }

    def _require_engine(self) -> Engine:
        if self.engine is None:
            raise RuntimeError("engine not running")
        return self.engine

    def _require_backend(self) -> RGBBackend:
        if self.backend is None:
            raise RuntimeError("backend not connected")
        return self.backend


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def run_daemon(path: Path | None = None, backend_name: str | None = None) -> int:
    """Run the daemon in the foreground until Ctrl+C."""
    daemon = Daemon(path, backend_name)
    daemon.start()
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0
