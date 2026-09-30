"""Effect engine: renders frames and pushes them to the backend."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .backends import RGBBackend
from .color import RGB
from .effects import Effect, EffectContext, create_effect, needs_audio, needs_cpu
from .sources import AudioSource, CPUSource

log = logging.getLogger(__name__)

FrameHook = Callable[[list[RGB]], None]


class Engine:
    """Drives one effect over a set of devices at a fixed frame rate."""

    def __init__(
        self,
        backend: RGBBackend,
        cfg: dict[str, Any],
        effect: Effect | None = None,
    ) -> None:
        self.backend = backend
        self.cfg = cfg
        general = cfg.get("general", {})
        self.fps = max(1, int(general.get("fps", 30)))
        self.brightness = float(general.get("brightness", 1.0))
        self._effect = effect or create_effect(general.get("effect", "breathing"), cfg)

        self._targets: list[int] = self._resolve_targets(general.get("devices") or [])
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        self.cpu = CPUSource()
        self.audio = AudioSource(
            device=_audio_device(cfg),
            samplerate=int(cfg.get("audio", {}).get("samplerate", 48000)),
            blocksize=int(cfg.get("audio", {}).get("blocksize", 512)),
        )
        self.frame_hook: FrameHook | None = None
        self.error: str | None = None
        self.frames_rendered = 0
        self.last_color: RGB | None = None

    # ------------------------------------------------------------------
    def _resolve_targets(self, wanted: list[Any]) -> list[int]:
        infos = self.backend.devices()
        if not wanted:
            return [i.index for i in infos]
        out: list[int] = []
        for item in wanted:
            if isinstance(item, int) or str(item).isdigit():
                idx = int(item)
                if any(i.index == idx for i in infos):
                    out.append(idx)
                continue
            needle = str(item).lower()
            out.extend(i.index for i in infos if needle in i.name.lower())
        return sorted(set(out)) or [i.index for i in infos]

    # -- properties ----------------------------------------------------
    @property
    def effect(self) -> Effect:
        with self._lock:
            return self._effect

    def set_effect(self, effect: Effect | str, **params: Any) -> Effect:
        if isinstance(effect, str):
            effect = create_effect(effect, self.cfg, **params)
        with self._lock:
            self._effect = effect
        self._sync_sources()
        return effect

    def set_param(self, key: str, value: Any) -> None:
        with self._lock:
            self._effect.set_param(key, value)

    def set_brightness(self, value: float) -> None:
        self.brightness = max(0.0, min(1.0, float(value)))

    def set_devices(self, indices: list[int]) -> None:
        with self._lock:
            self._targets = sorted(set(indices)) or [i.index for i in self.backend.devices()]

    @property
    def targets(self) -> list[int]:
        with self._lock:
            return list(self._targets)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- lifecycle -----------------------------------------------------
    def _sync_sources(self) -> None:
        effect = self.effect
        if needs_cpu(effect):
            self.cpu.start()
        if needs_audio(effect):
            self.audio.start()

    def start(self) -> None:
        if self.running:
            return
        self.error = None
        self._sync_sources()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pyrgb-engine", daemon=True)
        self._thread.start()

    def stop(self, clear: bool = False) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        self.cpu.stop()
        self.audio.stop()
        if clear:
            try:
                self.backend.clear()
            except Exception:  # pragma: no cover
                pass

    # -- render loop ---------------------------------------------------
    def _run(self) -> None:
        period = 1.0 / self.fps
        start = time.perf_counter()
        prev = start
        ctx = EffectContext(cpu=self.cpu, audio=self.audio)
        infos = {i.index: i for i in self.backend.devices()}

        while not self._stop.is_set():
            now = time.perf_counter()
            ctx.t = now - start
            ctx.dt = max(0.0, now - prev)
            ctx.brightness = self.brightness
            prev = now

            with self._lock:
                effect = self._effect
                targets = list(self._targets)

            first_frame: list[RGB] | None = None
            for idx in targets:
                info = infos.get(idx)
                n = info.led_count if info else 1
                try:
                    colors = effect.render_frame(max(1, n), ctx)
                    self.backend.set_colors(idx, colors)
                    if first_frame is None:
                        first_frame = colors
                except Exception as exc:
                    self.error = str(exc)
                    log.error("device %s update failed: %s", idx, exc)
                    self._stop.set()
                    break

            ctx.frame += 1
            self.frames_rendered = ctx.frame
            if first_frame:
                self.last_color = first_frame[0]
            if first_frame is not None and self.frame_hook is not None:
                try:
                    self.frame_hook(first_frame)
                except Exception:  # pragma: no cover - UI hook must not kill loop
                    pass

            elapsed = time.perf_counter() - now
            self._stop.wait(max(0.0, period - elapsed))

    # -- one-shot ------------------------------------------------------
    def apply_once(self, effect: Effect | None = None) -> list[RGB]:
        """Render and push a single frame (used by ``pyrgb set``)."""
        eff = effect or self.effect
        ctx = EffectContext(cpu=self.cpu, audio=self.audio, brightness=self.brightness)
        last: list[RGB] = []
        infos = {i.index: i for i in self.backend.devices()}
        for idx in self.targets:
            info = infos.get(idx)
            last = eff.render_frame(max(1, info.led_count if info else 1), ctx)
            self.backend.set_colors(idx, last)
        return last

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _audio_device(cfg: dict[str, Any]) -> str | int | None:
    raw = cfg.get("audio", {}).get("device", "")
    if raw in ("", None):
        return None
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return raw
