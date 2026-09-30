"""Effect implementations and registry.

An effect renders a list of ``RGB`` values for ``n`` LEDs given an
``EffectContext`` holding elapsed time and live signal sources.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .color import BLACK, RGB, gradient
from .sources import AudioSource, CPUSource


@dataclass(slots=True)
class EffectContext:
    t: float = 0.0                       # seconds since engine start
    dt: float = 0.0                      # seconds since previous frame
    frame: int = 0
    cpu: CPUSource | None = None
    audio: AudioSource | None = None
    brightness: float = 1.0

    @property
    def cpu_load(self) -> float:
        return self.cpu.value if self.cpu is not None else 0.0

    @property
    def audio_level(self) -> float:
        return self.audio.level if self.audio is not None else 0.0


class Effect:
    """Base class for all effects."""

    name = "base"
    description = ""
    #: option name -> (type, default, min, max) used by the GUI/CLI
    options: dict[str, tuple[str, Any, Any, Any]] = {}

    def __init__(self, **params: Any) -> None:
        self.params: dict[str, Any] = {}
        for key, (kind, default, *_rest) in self.options.items():
            self.params[key] = _coerce(kind, params.get(key, default))
        for key, value in params.items():
            if key not in self.options:
                self.params[key] = value
        self.reset()

    # -- helpers -------------------------------------------------------
    def reset(self) -> None:
        return None

    def set_param(self, key: str, value: Any) -> None:
        if key in self.options:
            value = _coerce(self.options[key][0], value)
        self.params[key] = value

    def color(self, key: str) -> RGB:
        return RGB.parse(self.params[key])

    def num(self, key: str) -> float:
        return float(self.params[key])

    # -- rendering -----------------------------------------------------
    def render(self, n: int, ctx: EffectContext) -> list[RGB]:
        raise NotImplementedError

    def render_frame(self, n: int, ctx: EffectContext) -> list[RGB]:
        colors = self.render(max(1, n), ctx)
        b = max(0.0, min(1.0, ctx.brightness))
        if b >= 0.999:
            return colors
        return [c.scaled(b) for c in colors]

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} {self.params}>"


# ---------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------

class StaticEffect(Effect):
    name = "static"
    description = "One solid color."
    options = {"color": ("color", "#00aaff", None, None)}

    def render(self, n: int, ctx: EffectContext) -> list[RGB]:
        return [self.color("color")] * n


class BreathingEffect(Effect):
    name = "breathing"
    description = "Smooth sine fade in and out."
    options = {
        "color": ("color", "#00aaff", None, None),
        "speed": ("float", 0.5, 0.02, 5.0),
        "min_brightness": ("float", 0.05, 0.0, 1.0),
    }

    def render(self, n: int, ctx: EffectContext) -> list[RGB]:
        phase = 0.5 - 0.5 * math.cos(2 * math.pi * self.num("speed") * ctx.t)
        lo = self.num("min_brightness")
        level = lo + (1.0 - lo) * (phase ** 1.6)
        return [self.color("color").scaled(level)] * n


class RainbowEffect(Effect):
    name = "rainbow"
    description = "Hue cycling wave across the LEDs."
    options = {
        "speed": ("float", 0.2, 0.01, 3.0),
        "spread": ("float", 1.0, 0.0, 4.0),
        "saturation": ("float", 1.0, 0.0, 1.0),
    }

    def render(self, n: int, ctx: EffectContext) -> list[RGB]:
        base = self.num("speed") * ctx.t
        spread = self.num("spread")
        sat = self.num("saturation")
        if n == 1 or spread <= 0.0:
            return [RGB.from_hsv(base, sat, 1.0)] * n
        return [RGB.from_hsv(base + spread * i / n, sat, 1.0) for i in range(n)]


class CPUEffect(Effect):
    name = "cpu"
    description = "Color follows CPU usage (cold -> warm -> hot)."
    options = {
        "cold": ("color", "#00ff66", None, None),
        "warm": ("color", "#ffcc00", None, None),
        "hot": ("color", "#ff1000", None, None),
        "smoothing": ("float", 0.25, 0.0, 0.95),
        "pulse": ("bool", False, None, None),
    }

    def reset(self) -> None:
        self._value = 0.0

    def render(self, n: int, ctx: EffectContext) -> list[RGB]:
        target = max(0.0, min(1.0, ctx.cpu_load))
        a = self.num("smoothing")
        self._value = self._value * a + target * (1.0 - a)
        col = gradient([self.color("cold"), self.color("warm"), self.color("hot")], self._value)
        if self.params.get("pulse"):
            rate = 0.6 + 3.4 * self._value
            wave = 0.55 + 0.45 * math.sin(2 * math.pi * rate * ctx.t)
            col = col.scaled(wave)
        if n <= 1:
            return [col]
        # bar-graph style: lit LEDs proportional to load
        lit = self._value * n
        out: list[RGB] = []
        for i in range(n):
            fill = max(0.0, min(1.0, lit - i))
            out.append(col.scaled(0.12 + 0.88 * fill))
        return out


class AudioEffect(Effect):
    name = "audio"
    description = "Reacts to the sound coming out of your speakers."
    options = {
        "color": ("color", "#00ff9d", None, None),
        "peak_color": ("color", "#ff0055", None, None),
        "gain": ("float", 1.0, 0.1, 6.0),
        "smoothing": ("float", 0.35, 0.0, 0.95),
        "floor": ("float", 0.02, 0.0, 0.5),
    }

    def reset(self) -> None:
        self._value = 0.0

    def render(self, n: int, ctx: EffectContext) -> list[RGB]:
        raw = max(0.0, min(1.0, ctx.audio_level * self.num("gain")))
        a = self.num("smoothing")
        self._value = max(raw, self._value * a + raw * (1.0 - a))
        level = max(self.num("floor"), self._value)
        col = self.color("color").blend(self.color("peak_color"), level ** 2)
        if n <= 1:
            return [col.scaled(level)]
        lit = level * n
        out: list[RGB] = []
        for i in range(n):
            fill = max(0.0, min(1.0, lit - i))
            out.append(col.scaled(max(self.num("floor"), fill)))
        return out


class StrobeEffect(Effect):
    name = "strobe"
    description = "Hard on/off flashing."
    options = {
        "color": ("color", "#ffffff", None, None),
        "speed": ("float", 6.0, 0.2, 30.0),
        "duty": ("float", 0.35, 0.02, 0.98),
    }

    def render(self, n: int, ctx: EffectContext) -> list[RGB]:
        period = 1.0 / max(0.01, self.num("speed"))
        on = (ctx.t % period) < period * self.num("duty")
        return [self.color("color") if on else BLACK] * n


class OffEffect(Effect):
    name = "off"
    description = "Turn everything off."

    def render(self, n: int, ctx: EffectContext) -> list[RGB]:
        return [BLACK] * n


class RandomEffect(Effect):
    name = "random"
    description = "Random color fades."
    options = {"speed": ("float", 0.6, 0.05, 6.0), "per_led": ("bool", False, None, None)}

    def reset(self) -> None:
        self._from = [RGB.from_hsv(random.random(), 1.0, 1.0)]
        self._to = [RGB.from_hsv(random.random(), 1.0, 1.0)]
        self._phase = 0.0
        self._n = 1

    def render(self, n: int, ctx: EffectContext) -> list[RGB]:
        count = n if self.params.get("per_led") else 1
        if count != self._n:
            self._n = count
            self._from = [RGB.from_hsv(random.random(), 1.0, 1.0) for _ in range(count)]
            self._to = [RGB.from_hsv(random.random(), 1.0, 1.0) for _ in range(count)]
        self._phase += ctx.dt * self.num("speed")
        while self._phase >= 1.0:
            self._phase -= 1.0
            self._from = self._to
            self._to = [RGB.from_hsv(random.random(), 1.0, 1.0) for _ in range(self._n)]
        blended = [f.blend(t, self._phase) for f, t in zip(self._from, self._to)]
        if count == 1:
            return blended * n
        return blended


REGISTRY: dict[str, type[Effect]] = {
    cls.name: cls
    for cls in (
        StaticEffect,
        BreathingEffect,
        RainbowEffect,
        CPUEffect,
        AudioEffect,
        StrobeEffect,
        RandomEffect,
        OffEffect,
    )
}


def effect_names() -> list[str]:
    return list(REGISTRY)


def create_effect(name: str, cfg: dict[str, Any] | None = None, **overrides: Any) -> Effect:
    key = (name or "").strip().lower()
    if key not in REGISTRY:
        raise KeyError(f"unknown effect {name!r} (available: {', '.join(REGISTRY)})")
    params: dict[str, Any] = {}
    if cfg:
        params.update(cfg.get("effects", {}).get(key, {}) or {})
    params.update({k: v for k, v in overrides.items() if v is not None})
    return REGISTRY[key](**params)


def needs_cpu(effect: Effect) -> bool:
    return isinstance(effect, CPUEffect)


def needs_audio(effect: Effect) -> bool:
    return isinstance(effect, AudioEffect)


def _coerce(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "float":
        return float(value)
    if kind == "int":
        return int(float(value))
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if kind == "color":
        return RGB.parse(value).to_hex()
    return value


def describe(names: Iterable[str] | None = None) -> str:
    lines = []
    for name in names or REGISTRY:
        cls = REGISTRY[name]
        opts = ", ".join(f"{k}={v[1]}" for k, v in cls.options.items()) or "-"
        lines.append(f"{name:<10} {cls.description}\n{'':<10} options: {opts}")
    return "\n".join(lines)


EffectFactory = Callable[..., Effect]
