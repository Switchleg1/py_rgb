"""Software-only backend: no hardware, used for preview/testing/fallback."""

from __future__ import annotations

from ..color import BLACK, RGB
from .base import DeviceInfo, RGBBackend

DEFAULT_LAYOUT = [
    ("Virtual Motherboard", 12, "motherboard"),
    ("Virtual RAM", 8, "dram"),
]


class DummyBackend(RGBBackend):
    """Keeps the last frame in memory so the GUI/CLI still work headless."""

    name = "dummy"

    def __init__(self, layout: list[tuple[str, int, str]] | None = None) -> None:
        self._layout = layout or DEFAULT_LAYOUT
        self._infos: list[DeviceInfo] = []
        self.frames: dict[int, list[RGB]] = {}

    def connect(self) -> None:
        self._infos = [
            DeviceInfo(index=i, name=name, led_count=leds, kind=kind, zones=["zone 0"])
            for i, (name, leds, kind) in enumerate(self._layout)
        ]
        self.frames = {info.index: [BLACK] * info.led_count for info in self._infos}

    def devices(self) -> list[DeviceInfo]:
        return list(self._infos)

    def set_colors(self, device_index: int, colors: list[RGB]) -> None:
        self.frames[device_index] = list(colors)

    def last_color(self, device_index: int = 0) -> RGB:
        frame = self.frames.get(device_index) or [BLACK]
        return frame[0]
