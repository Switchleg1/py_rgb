"""Color primitives and helpers."""

from __future__ import annotations

import colorsys
from dataclasses import dataclass


def _clamp8(v: int) -> int:
    return 0 if v < 0 else 255 if v > 255 else int(v)


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else float(v)


@dataclass(frozen=True, slots=True)
class RGB:
    r: int
    g: int
    b: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "r", _clamp8(self.r))
        object.__setattr__(self, "g", _clamp8(self.g))
        object.__setattr__(self, "b", _clamp8(self.b))

    # ---------- constructors ----------
    @classmethod
    def from_hex(cls, value: str) -> "RGB":
        s = value.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            raise ValueError(f"invalid hex color: {value!r}")
        try:
            n = int(s, 16)
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"invalid hex color: {value!r}") from exc
        return cls((n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF)

    @classmethod
    def from_hsv(cls, h: float, s: float, v: float) -> "RGB":
        r, g, b = colorsys.hsv_to_rgb(h % 1.0, _clamp01(s), _clamp01(v))
        return cls(round(r * 255), round(g * 255), round(b * 255))

    @classmethod
    def parse(cls, value: "str | RGB | tuple[int, int, int] | list[int]") -> "RGB":
        if isinstance(value, RGB):
            return value
        if isinstance(value, (tuple, list)):
            if len(value) != 3:
                raise ValueError(f"invalid color sequence: {value!r}")
            return cls(int(value[0]), int(value[1]), int(value[2]))
        s = str(value).strip()
        if s.lower() in NAMED_COLORS:
            return NAMED_COLORS[s.lower()]
        if "," in s:
            parts = [p for p in s.replace("(", "").replace(")", "").split(",") if p.strip()]
            if len(parts) == 3:
                return cls(int(parts[0]), int(parts[1]), int(parts[2]))
        return cls.from_hex(s)

    # ---------- conversions ----------
    def to_hex(self) -> str:
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}"

    def to_hsv(self) -> tuple[float, float, float]:
        return colorsys.rgb_to_hsv(self.r / 255, self.g / 255, self.b / 255)

    def to_tuple(self) -> tuple[int, int, int]:
        return (self.r, self.g, self.b)

    # ---------- math ----------
    def scaled(self, factor: float) -> "RGB":
        f = max(0.0, factor)
        return RGB(round(self.r * f), round(self.g * f), round(self.b * f))

    def blend(self, other: "RGB", t: float) -> "RGB":
        t = _clamp01(t)
        return RGB(
            round(self.r + (other.r - self.r) * t),
            round(self.g + (other.g - self.g) * t),
            round(self.b + (other.b - self.b) * t),
        )

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.to_hex()


BLACK = RGB(0, 0, 0)
WHITE = RGB(255, 255, 255)

NAMED_COLORS: dict[str, RGB] = {
    "black": BLACK,
    "white": WHITE,
    "red": RGB(255, 0, 0),
    "green": RGB(0, 255, 0),
    "blue": RGB(0, 0, 255),
    "yellow": RGB(255, 255, 0),
    "cyan": RGB(0, 255, 255),
    "magenta": RGB(255, 0, 255),
    "orange": RGB(255, 96, 0),
    "purple": RGB(128, 0, 255),
    "pink": RGB(255, 64, 160),
    "lime": RGB(128, 255, 0),
    "teal": RGB(0, 192, 160),
}


def gradient(stops: list[RGB], t: float) -> RGB:
    """Sample a multi-stop gradient at position ``t`` in [0, 1]."""
    if not stops:
        return BLACK
    if len(stops) == 1:
        return stops[0]
    t = _clamp01(t)
    span = 1.0 / (len(stops) - 1)
    idx = min(int(t / span), len(stops) - 2)
    local = (t - idx * span) / span
    return stops[idx].blend(stops[idx + 1], local)
