"""Backend registry."""

from __future__ import annotations

import logging
from typing import Any

from .base import BackendError, DeviceInfo, RGBBackend
from .composite import CompositeBackend
from .dummy_backend import DummyBackend
from .msi_mystic import MSIMysticBackend
from .openrgb_backend import OpenRGBBackend

log = logging.getLogger(__name__)

BACKEND_NAMES = ("auto", "openrgb", "msi", "dummy")


def _make_openrgb(cfg: dict[str, Any]) -> RGBBackend:
    orgb = cfg.get("openrgb", {})
    backend = OpenRGBBackend(
        host=orgb.get("host", "127.0.0.1"),
        port=int(orgb.get("port", 6742)),
        client_name=orgb.get("client_name", "py_rgb"),
        force_direct_mode=bool(orgb.get("force_direct_mode", True)),
    )
    backend.connect()
    return backend


def _make_msi(cfg: dict[str, Any]) -> RGBBackend:
    msi = cfg.get("msi", {})
    backend = MSIMysticBackend(
        brightness_level=int(msi.get("brightness_level", 5)),
        save_to_flash=bool(msi.get("save_to_flash", False)),
        max_hz=float(msi.get("max_hz", 25.0)),
        zones=[int(z) for z in msi.get("zones", []) or []],
    )
    backend.connect()
    return backend


def _make_dummy(cfg: dict[str, Any]) -> RGBBackend:
    backend = DummyBackend()
    backend.connect()
    return backend


def create_backend(cfg: dict[str, Any], name: str | None = None) -> RGBBackend:
    """Create and connect a backend.

    ``auto`` combines every backend that is actually available (OpenRGB for
    peripherals/RAM plus the native MSI Mystic Light controller) and falls back
    to the dummy backend when nothing is reachable.
    """
    choice = (name or cfg.get("general", {}).get("backend", "auto") or "auto").lower()

    if choice == "openrgb":
        return _make_openrgb(cfg)
    if choice == "msi":
        return _make_msi(cfg)
    if choice == "dummy":
        return _make_dummy(cfg)
    if choice != "auto":
        raise BackendError(
            f"unknown backend {choice!r} (expected one of {', '.join(BACKEND_NAMES)})"
        )

    found: list[RGBBackend] = []
    problems: list[str] = []
    for label, factory in (("MSI Mystic Light", _make_msi), ("OpenRGB", _make_openrgb)):
        try:
            backend = factory(cfg)
        except (BackendError, Exception) as exc:
            # probing is expected to fail for absent hardware - keep it quiet
            problems.append(f"{label}: {exc}")
            log.debug("%s unavailable: %s", label, exc)
            continue
        if backend.devices():
            found.append(backend)
        else:
            problems.append(f"{label}: connected but reported no devices")
            log.debug("%s reported no devices", label)
            backend.close()

    if not found:
        for problem in problems:
            log.warning("%s", problem)
        log.warning("no RGB hardware reachable; falling back to the dummy backend (see 'pyrgb doctor')")
        return _make_dummy(cfg)
    if len(found) == 1:
        return found[0]
    return CompositeBackend(found)


__all__ = [
    "BACKEND_NAMES",
    "BackendError",
    "CompositeBackend",
    "DeviceInfo",
    "DummyBackend",
    "MSIMysticBackend",
    "OpenRGBBackend",
    "RGBBackend",
    "create_backend",
]
