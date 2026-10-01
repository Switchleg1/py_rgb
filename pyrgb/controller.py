"""Uniform control surface used by the GUI.

Only one process may own the LEDs.  When the daemon/service is running the GUI
must not open the hardware itself, so it drives a :class:`RemoteController`
which edits ``config.toml`` and talks to the daemon over the control channel.
Without a daemon the GUI falls back to a :class:`LocalController` that owns an
engine in-process, exactly like ``pyrgb run``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .backends import RGBBackend, create_backend
from .color import BLACK, RGB
from .config import config_path, load_config, save_config
from .effects import create_effect
from .engine import Engine
from .ipc import daemon_status, send_command


class Controller(ABC):
    """What the GUI needs, regardless of where the engine lives."""

    mode: str = "local"

    @property
    @abstractmethod
    def backend_name(self) -> str: ...

    @abstractmethod
    def devices(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def status(self) -> dict[str, Any]: ...

    @abstractmethod
    def set_effect(self, name: str, params: dict[str, Any] | None = None) -> None: ...

    @abstractmethod
    def set_param(self, key: str, value: Any) -> None: ...

    @abstractmethod
    def set_brightness(self, value: float) -> None: ...

    @abstractmethod
    def set_devices(self, indices: list[int]) -> None: ...

    @abstractmethod
    def set_fps(self, fps: int) -> None: ...

    @abstractmethod
    def resume(self) -> None: ...

    @abstractmethod
    def pause(self) -> None: ...

    @abstractmethod
    def all_off(self) -> None: ...

    @abstractmethod
    def apply_config(self, cfg: dict[str, Any], path: Path | None = None) -> Path:
        """Persist the config and make the running engine pick it up."""

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------
# local (GUI owns the hardware)
# ---------------------------------------------------------------------

class LocalController(Controller):
    mode = "local"

    def __init__(self, cfg: dict[str, Any], backend_name: str | None = None) -> None:
        self.cfg = cfg
        self._backend: RGBBackend = create_backend(cfg, backend_name)
        self.engine = Engine(self._backend, cfg)

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def devices(self) -> list[dict[str, Any]]:
        return [
            {"index": d.index, "name": d.name, "leds": d.led_count, "kind": d.kind}
            for d in self._backend.devices()
        ]

    def status(self) -> dict[str, Any]:
        engine = self.engine
        return {
            "backend": self._backend.name,
            "running": engine.running,
            "paused": not engine.running,
            "effect": engine.effect.name,
            "params": dict(engine.effect.params),
            "brightness": engine.brightness,
            "fps": engine.fps,
            "devices": engine.targets,
            "frames": engine.frames_rendered,
            "cpu": engine.cpu.value,
            "temp": round(engine.temp.value, 1),
            "temp_provider": engine.temp.provider,
            "temp_available": engine.temp.available,
            "audio": engine.audio.level,
            "audio_mode": engine.audio.mode,
            "color": (engine.last_color or BLACK).to_hex(),
            "error": engine.error,
        }

    def set_effect(self, name: str, params: dict[str, Any] | None = None) -> None:
        self.engine.set_effect(create_effect(name, self.cfg, **(params or {})))
        if not self.engine.running:
            self.engine.start()

    def set_param(self, key: str, value: Any) -> None:
        self.engine.set_param(key, value)

    def set_brightness(self, value: float) -> None:
        self.engine.set_brightness(value)

    def set_devices(self, indices: list[int]) -> None:
        self.engine.set_devices(indices)

    def set_fps(self, fps: int) -> None:
        self.engine.fps = max(1, int(fps))

    def resume(self) -> None:
        if not self.engine.running:
            self.engine.start()

    def pause(self) -> None:
        self.engine.stop()

    def all_off(self) -> None:
        self.engine.stop()
        self.engine.apply_once(create_effect("off", self.cfg))

    def apply_config(self, cfg: dict[str, Any], path: Path | None = None) -> Path:
        self.cfg = cfg
        self.engine.cfg = cfg
        return save_config(cfg, path)

    def close(self) -> None:
        self.engine.stop()
        try:
            self._backend.close()
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------
# remote (daemon/service owns the hardware)
# ---------------------------------------------------------------------

class RemoteController(Controller):
    mode = "remote"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        daemon_cfg = cfg.get("daemon", {})
        self.host = daemon_cfg.get("host", "127.0.0.1")
        self.port = int(daemon_cfg.get("port", 6743))
        self.token = str(daemon_cfg.get("token", ""))
        self._last_status: dict[str, Any] = {}
        self.connected = True

    # -- plumbing ------------------------------------------------------
    def _send(self, command: str, **payload: Any) -> dict[str, Any]:
        try:
            reply = send_command(
                command, host=self.host, port=self.port, token=self.token, **payload
            )
            self.connected = True
            return reply
        except ConnectionError:
            self.connected = False
            return {"ok": False, "error": "daemon not reachable"}

    @classmethod
    def probe(cls, cfg: dict[str, Any]) -> "RemoteController | None":
        daemon_cfg = cfg.get("daemon", {})
        status = daemon_status(
            daemon_cfg.get("host", "127.0.0.1"),
            int(daemon_cfg.get("port", 6743)),
            str(daemon_cfg.get("token", "")),
        )
        if status is None:
            return None
        ctl = cls(cfg)
        ctl._last_status = status
        return ctl

    # -- Controller API ------------------------------------------------
    @property
    def backend_name(self) -> str:
        return f"{self._last_status.get('backend', '?')} (daemon)"

    def devices(self) -> list[dict[str, Any]]:
        status = self.status()
        devices = status.get("all_devices") or []
        if devices:
            return devices
        return [
            {"index": i, "name": name, "leds": 1, "kind": "unknown"}
            for i, name in enumerate(status.get("device_names", []))
        ]

    def status(self) -> dict[str, Any]:
        reply = self._send("status")
        if reply.get("ok"):
            reply.pop("ok", None)
            self._last_status = reply
        return self._last_status

    def set_effect(self, name: str, params: dict[str, Any] | None = None) -> None:
        self._send("effect", name=name, params=params or {})

    def set_param(self, key: str, value: Any) -> None:
        name = self._last_status.get("effect")
        params = dict(self._last_status.get("params") or {})
        params[key] = value
        if name:
            self._send("effect", name=name, params=params)
            self._last_status["params"] = params

    def set_brightness(self, value: float) -> None:
        self._send("brightness", value=value)

    def set_devices(self, indices: list[int]) -> None:
        # device selection lives in the config file
        self.cfg.setdefault("general", {})["devices"] = list(indices)
        self.apply_config(self.cfg)

    def set_fps(self, fps: int) -> None:
        self.cfg.setdefault("general", {})["fps"] = int(fps)
        self.apply_config(self.cfg)

    def resume(self) -> None:
        self._send("on")

    def pause(self) -> None:
        self._send("off")

    def all_off(self) -> None:
        self._send("off")

    def apply_config(self, cfg: dict[str, Any], path: Path | None = None) -> Path:
        self.cfg = cfg
        written = save_config(cfg, path)
        self._send("reload")
        return written

    def shutdown_daemon(self) -> None:
        self._send("shutdown")


# ---------------------------------------------------------------------

def make_controller(
    cfg: dict[str, Any] | None = None,
    backend_name: str | None = None,
    prefer_remote: bool = True,
) -> Controller:
    """Attach to a running daemon when there is one, else drive locally."""
    cfg = cfg if cfg is not None else load_config()
    if prefer_remote:
        remote = RemoteController.probe(cfg)
        if remote is not None:
            return remote
    return LocalController(cfg, backend_name)


def current_config_path() -> Path:
    return config_path()


__all__ = [
    "Controller",
    "LocalController",
    "RemoteController",
    "make_controller",
    "current_config_path",
    "RGB",
]
