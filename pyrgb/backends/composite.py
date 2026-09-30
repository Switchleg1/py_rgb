"""Combine several backends behind one flat device list."""

from __future__ import annotations

from ..color import RGB
from .base import BackendError, DeviceInfo, RGBBackend


class CompositeBackend(RGBBackend):
    """Presents devices from multiple backends with one continuous index space."""

    def __init__(self, backends: list[RGBBackend]) -> None:
        self._backends = list(backends)
        self._map: dict[int, tuple[RGBBackend, int]] = {}
        self._infos: list[DeviceInfo] = []
        self._rebuild()

    @property
    def name(self) -> str:  # type: ignore[override]
        return "+".join(b.name for b in self._backends) or "empty"

    def _rebuild(self) -> None:
        self._map.clear()
        self._infos.clear()
        idx = 0
        for backend in self._backends:
            for info in backend.devices():
                self._infos.append(
                    DeviceInfo(
                        index=idx,
                        name=info.name,
                        led_count=info.led_count,
                        kind=info.kind,
                        zones=info.zones,
                    )
                )
                self._map[idx] = (backend, info.index)
                idx += 1

    def connect(self) -> None:
        for backend in self._backends:
            backend.connect()
        self._rebuild()

    def devices(self) -> list[DeviceInfo]:
        return list(self._infos)

    def set_colors(self, device_index: int, colors: list[RGB]) -> None:
        target = self._map.get(device_index)
        if target is None:
            raise BackendError(f"no device with index {device_index}")
        backend, local = target
        backend.set_colors(local, colors)

    def close(self) -> None:
        for backend in self._backends:
            try:
                backend.close()
            except Exception:  # pragma: no cover
                pass

    def sub_backends(self) -> list[RGBBackend]:
        return list(self._backends)
