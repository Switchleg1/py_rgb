"""OpenRGB SDK backend (works with the OpenRGB server on Windows/Linux)."""

from __future__ import annotations

from typing import Any

from ..color import RGB
from .base import BackendError, DeviceInfo, RGBBackend


class OpenRGBBackend(RGBBackend):
    name = "openrgb"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6742,
        client_name: str = "py_rgb",
        force_direct_mode: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.client_name = client_name
        self.force_direct_mode = force_direct_mode
        self._client: Any = None
        self._devices: list[Any] = []
        self._infos: list[DeviceInfo] = []

    # ------------------------------------------------------------------
    def connect(self) -> None:
        try:
            from openrgb import OpenRGBClient
        except ImportError as exc:  # pragma: no cover
            raise BackendError(
                "openrgb-python is not installed (pip install openrgb-python)"
            ) from exc

        try:
            self._client = OpenRGBClient(self.host, self.port, self.client_name)
        except Exception as exc:
            raise BackendError(
                f"cannot reach the OpenRGB server at {self.host}:{self.port} "
                "- start OpenRGB with 'Enable SDK server' checked"
            ) from exc

        self._devices = list(self._client.devices)
        self._infos = []
        for idx, dev in enumerate(self._devices):
            if self.force_direct_mode:
                self._try_direct_mode(dev)
            self._infos.append(
                DeviceInfo(
                    index=idx,
                    name=str(getattr(dev, "name", f"device{idx}")),
                    led_count=len(getattr(dev, "leds", []) or []),
                    kind=str(getattr(getattr(dev, "type", None), "name", "unknown")).lower(),
                    zones=[str(z.name) for z in getattr(dev, "zones", []) or []],
                )
            )

    @staticmethod
    def _try_direct_mode(dev: Any) -> None:
        for mode in ("direct", "Direct", "static", "Static"):
            try:
                dev.set_mode(mode)
                return
            except Exception:
                continue

    # ------------------------------------------------------------------
    def devices(self) -> list[DeviceInfo]:
        return list(self._infos)

    def set_colors(self, device_index: int, colors: list[RGB]) -> None:
        if self._client is None:
            raise BackendError("backend not connected")
        try:
            dev = self._devices[device_index]
        except IndexError as exc:
            raise BackendError(f"no device with index {device_index}") from exc

        from openrgb.utils import RGBColor

        count = len(getattr(dev, "leds", []) or []) or len(colors)
        payload = _fit(colors, count)
        try:
            dev.set_colors([RGBColor(c.r, c.g, c.b) for c in payload], fast=True)
        except TypeError:  # older API without fast kwarg
            dev.set_colors([RGBColor(c.r, c.g, c.b) for c in payload])
        except Exception as exc:
            raise BackendError(f"failed to update '{getattr(dev, 'name', device_index)}': {exc}") from exc

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # pragma: no cover
                pass


def _fit(colors: list[RGB], count: int) -> list[RGB]:
    if not colors:
        from ..color import BLACK

        return [BLACK] * count
    if len(colors) == count:
        return colors
    if len(colors) > count:
        return colors[:count]
    out = list(colors)
    while len(out) < count:
        out.append(colors[len(out) % len(colors)])
    return out
