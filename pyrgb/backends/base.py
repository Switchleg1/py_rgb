"""Backend interface for RGB hardware."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..color import RGB


@dataclass(slots=True)
class DeviceInfo:
    index: int
    name: str
    led_count: int
    kind: str = "unknown"
    zones: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.index}] {self.name} ({self.kind}, {self.led_count} LEDs)"


class RGBBackend(ABC):
    """Common interface implemented by every hardware backend."""

    name: str = "base"

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def devices(self) -> list[DeviceInfo]: ...

    @abstractmethod
    def set_colors(self, device_index: int, colors: list[RGB]) -> None:
        """Apply per-LED colors to one device."""

    def set_all(self, device_index: int, color: RGB) -> None:
        info = self.devices()[device_index]
        self.set_colors(device_index, [color] * max(1, info.led_count))

    def clear(self) -> None:
        from ..color import BLACK

        for info in self.devices():
            try:
                self.set_colors(info.index, [BLACK] * max(1, info.led_count))
            except Exception:  # pragma: no cover - best effort
                pass

    def close(self) -> None:
        return None

    def __enter__(self) -> "RGBBackend":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class BackendError(RuntimeError):
    """Raised when a backend cannot connect or talk to hardware."""
