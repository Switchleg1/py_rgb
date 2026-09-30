"""Local TOML configuration stored in the project root folder."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

CONFIG_FILENAME = "config.toml"

#: Root folder of the project (the folder that contains the ``pyrgb`` package).
ROOT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG: dict[str, Any] = {
    "general": {
        "backend": "auto",          # auto | openrgb | dummy
        "fps": 30,
        "brightness": 1.0,          # global 0..1 multiplier
        "effect": "breathing",      # startup effect
        "devices": [],              # empty list = all detected devices
        "autostart": True,          # GUI starts the engine immediately
    },
    "daemon": {
        "host": "127.0.0.1",
        "port": 6743,
        "token": "",               # optional shared secret for the control channel
        "watch_config": True,      # auto-reload when config.toml changes on disk
        "clear_on_exit": False,    # turn LEDs off when the daemon stops
    },
    "gui": {
        "tray": True,              # show a taskbar tray icon
        "start_minimized": False,  # launch straight to the tray
        "close_to_tray": True,     # closing the window hides it instead of quitting
    },
    "msi": {
        # native MSI Mystic Light controller (USB 0db0:0076)
        "brightness_level": 5,      # hardware brightness 1..5
        "save_to_flash": False,     # True writes each color to controller flash
        "max_hz": 25.0,             # cap on HID writes per second
        "zones": [],                # empty = all 18 zone records (needed on Z890)
    },
    "openrgb": {
        "host": "127.0.0.1",
        "port": 6742,
        "client_name": "py_rgb",
        "force_direct_mode": True,
    },
    "effects": {
        "static": {"color": "#00aaff"},
        "breathing": {"color": "#00aaff", "speed": 0.5, "min_brightness": 0.05},
        "rainbow": {"speed": 0.2, "spread": 1.0, "saturation": 1.0},
        "cpu": {
            "cold": "#00ff66",
            "warm": "#ffcc00",
            "hot": "#ff1000",
            "smoothing": 0.25,
            "pulse": False,
        },
        "audio": {
            "color": "#00ff9d",
            "peak_color": "#ff0055",
            "gain": 1.0,
            "smoothing": 0.35,
            "floor": 0.02,
        },
        "strobe": {"color": "#ffffff", "speed": 6.0, "duty": 0.35},
        "random": {"speed": 0.6, "per_led": False},
    },
    "audio": {
        "device": "",               # blank = auto (default speaker loopback)
        "samplerate": 48000,
        "blocksize": 512,
    },
}


def config_path() -> Path:
    """Path of the local config file (overridable with ``PYRGB_CONFIG``)."""
    env = os.environ.get("PYRGB_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return ROOT_DIR / CONFIG_FILENAME


def default_config() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load config merged over the defaults. Missing file -> defaults."""
    p = path or config_path()
    if not p.is_file():
        return default_config()
    if tomllib is None:  # pragma: no cover
        raise RuntimeError("tomllib unavailable; Python 3.11+ required")
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    return deep_merge(DEFAULT_CONFIG, data)


def save_config(cfg: dict[str, Any], path: Path | None = None) -> Path:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = dumps_toml(cfg, header="py_rgb configuration - edit freely, restart to apply")
    p.write_text(text, encoding="utf-8")
    return p


def get_path(cfg: dict[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def coerce_like_default(dotted: str, raw: str) -> Any:
    """Convert a CLI string to the type used by the default config entry."""
    current = get_path(DEFAULT_CONFIG, dotted)
    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


# --------------------------------------------------------------------------
# Minimal TOML writer (sufficient for this config schema)
# --------------------------------------------------------------------------

def dumps_toml(data: dict[str, Any], header: str | None = None) -> str:
    lines: list[str] = []
    if header:
        lines.append(f"# {header}")
        lines.append("")
    _emit_table(data, [], lines)
    return "\n".join(lines).rstrip() + "\n"


def _emit_table(table: dict[str, Any], prefix: list[str], lines: list[str]) -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in table.items() if isinstance(v, dict)}

    if prefix and (scalars or not tables):
        lines.append(f"[{'.'.join(prefix)}]")
    for key, value in scalars.items():
        lines.append(f"{_fmt_key(key)} = {_fmt_value(value)}")
    if prefix and (scalars or not tables):
        lines.append("")

    for key, value in tables.items():
        _emit_table(value, prefix + [key], lines)


def _fmt_key(key: str) -> str:
    if all(c.isalnum() or c in "_-" for c in key) and key:
        return key
    return _fmt_str(key)


def _fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        return repr(round(value, 6))
    if isinstance(value, str):
        return _fmt_str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt_value(v) for v in value) + "]"
    if value is None:
        return '""'
    return _fmt_str(str(value))


def _fmt_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
