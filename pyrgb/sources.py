"""Live signal sources: CPU load and system audio output level."""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)


class CPUSource:
    """Background CPU-usage sampler (0.0 .. 1.0)."""

    def __init__(self, interval: float = 0.25) -> None:
        self.interval = interval
        self._value = 0.0
        self._per_core: list[float] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.available = False

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            import psutil  # noqa: F401
        except ImportError:
            log.warning("psutil not installed; CPU effects will stay at 0%%")
            return
        self.available = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pyrgb-cpu", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import psutil

        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        while not self._stop.is_set():
            total = psutil.cpu_percent(interval=None) / 100.0
            cores = [c / 100.0 for c in psutil.cpu_percent(interval=None, percpu=True)]
            with self._lock:
                self._value = total
                self._per_core = cores
            self._stop.wait(self.interval)

    @property
    def value(self) -> float:
        with self._lock:
            return self._value

    @property
    def per_core(self) -> list[float]:
        with self._lock:
            return list(self._per_core)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)


LOOPBACK_HINTS = (
    "loopback",
    "stereo mix",
    "stereomix",
    "what u hear",
    "what you hear",
    "wave out mix",
    "mixage st",
)


class AudioSource:
    """System *output* loudness, so effects react to what you actually hear.

    Capture strategy - the first one that works wins:

    1. ``soundcard`` WASAPI loopback of the default speaker (best on Windows).
    2. ``sounddevice`` WASAPI loopback, if the installed PortAudio supports it.
    3. ``sounddevice`` on a "Stereo Mix"-style loopback input device.
    4. ``sounddevice`` on the default input device (microphone).

    ``level`` is a smoothed 0..1 loudness value, ``peak`` the raw block peak.
    """

    def __init__(
        self,
        device: str | int | None = None,
        samplerate: int = 48000,
        blocksize: int = 512,
        smoothing: float = 0.35,
    ) -> None:
        self.device = device if device not in ("", None) else None
        self.samplerate = int(samplerate)
        self.blocksize = int(blocksize)
        self.smoothing = smoothing
        self._level = 0.0
        self._peak = 0.0
        self._lock = threading.Lock()
        self._stream: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.available = False
        self.mode = "none"
        self.source_name = ""
        self.error: str | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self.available:
            return
        try:
            import numpy  # noqa: F401
        except ImportError as exc:
            self.error = f"numpy missing ({exc})"
            log.warning("audio capture unavailable: %s", self.error)
            return

        if self._start_soundcard():
            return
        if self._start_sounddevice():
            return
        log.warning("no usable audio capture source: %s", self.error)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.5)
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # pragma: no cover
                pass
        self.available = False
        self.mode = "none"
        with self._lock:
            self._level = 0.0
            self._peak = 0.0

    # ------------------------------------------------------------------
    # capture engines
    # ------------------------------------------------------------------
    def _start_soundcard(self) -> bool:
        try:
            import warnings

            import numpy as np
            import soundcard as sc
            from soundcard import SoundcardRuntimeWarning

            warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)
        except Exception as exc:
            self.error = f"soundcard unavailable ({exc})"
            return False

        try:
            if isinstance(self.device, str) and self.device:
                mic = sc.get_microphone(self.device, include_loopback=True)
            else:
                speaker = sc.default_speaker()
                mic = sc.get_microphone(str(speaker.name), include_loopback=True)
            name = str(getattr(mic, "name", "loopback"))
        except Exception as exc:
            self.error = f"soundcard loopback failed ({exc})"
            return False

        self._stop.clear()

        def worker() -> None:
            com = _com_initialize()
            try:
                with mic.recorder(
                    samplerate=self.samplerate, channels=2, blocksize=self.blocksize
                ) as rec:
                    while not self._stop.is_set():
                        block = rec.record(numframes=self.blocksize)
                        self._ingest(np.asarray(block, dtype="float32"))
            except Exception as exc:  # pragma: no cover - device dependent
                self.error = str(exc)
                self.available = False
                log.warning("loopback capture stopped: %s", exc)
            finally:
                if com:
                    _com_uninitialize()

        self._thread = threading.Thread(target=worker, name="pyrgb-audio", daemon=True)
        self._thread.start()
        self.available = True
        self.mode = "loopback (soundcard)"
        self.source_name = name
        self.error = None
        return True

    def _start_sounddevice(self) -> bool:
        try:
            import sounddevice as sd
        except ImportError as exc:
            self.error = f"sounddevice not installed ({exc})"
            return False

        for opener, mode in (
            (self._open_wasapi_loopback, "loopback (wasapi)"),
            (self._open_named_loopback, "loopback (stereo mix)"),
            (self._open_default_input, "input device"),
        ):
            try:
                stream, name = opener(sd)
            except Exception as exc:
                self.error = str(exc)
                continue
            if stream is None:
                continue
            try:
                stream.start()
            except Exception as exc:  # pragma: no cover - device dependent
                self.error = str(exc)
                try:
                    stream.close()
                except Exception:
                    pass
                continue
            self._stream = stream
            self.available = True
            self.mode = mode
            self.source_name = name
            self.error = None
            return True
        return False

    # -- stream builders -----------------------------------------------
    def _open_wasapi_loopback(self, sd: Any) -> tuple[Any, str]:
        try:
            settings = sd.WasapiSettings(loopback=True)  # type: ignore[call-arg]
        except TypeError as exc:  # PortAudio build without loopback support
            raise RuntimeError("WASAPI loopback not supported by this sounddevice build") from exc
        device = self.device if self.device is not None else _wasapi_default_output(sd)
        info = sd.query_devices(device)
        channels = max(1, int(info.get("max_output_channels") or 2))
        return self._make_stream(sd, device, channels, settings), str(info["name"])

    def _open_named_loopback(self, sd: Any) -> tuple[Any, str]:
        if self.device is not None:
            info = sd.query_devices(self.device)
            channels = max(1, int(info.get("max_input_channels") or 2))
            return self._make_stream(sd, self.device, channels, None), str(info["name"])
        for idx, dev in enumerate(sd.query_devices()):
            name = str(dev["name"]).lower()
            if dev.get("max_input_channels", 0) > 0 and any(h in name for h in LOOPBACK_HINTS):
                channels = max(1, int(dev["max_input_channels"]))
                return self._make_stream(sd, idx, channels, None), str(dev["name"])
        return None, ""

    def _open_default_input(self, sd: Any) -> tuple[Any, str]:
        device = self.device if self.device is not None else sd.default.device[0]
        info = sd.query_devices(device)
        channels = max(1, int(info.get("max_input_channels") or 0))
        if channels == 0:
            raise RuntimeError("default input device has no input channels")
        return self._make_stream(sd, device, channels, None), str(info["name"])

    def _make_stream(self, sd: Any, device: Any, channels: int, extra: Any) -> Any:
        import numpy as np

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            self._ingest(np.asarray(indata, dtype="float32"))

        return sd.InputStream(
            device=device,
            channels=channels,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            dtype="float32",
            callback=callback,
            extra_settings=extra,
        )

    # ------------------------------------------------------------------
    def _ingest(self, block: Any) -> None:
        import numpy as np

        if block is None or block.size == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(block))))
        peak = float(np.max(np.abs(block)))
        # map RMS to a perceptual 0..1 window (-55 dBFS .. -6 dBFS)
        if rms <= 1e-6:
            shaped = 0.0
        else:
            db = 20.0 * np.log10(rms)
            shaped = float(min(1.0, max(0.0, (db + 55.0) / 49.0)))
        a = max(0.0, min(1.0, self.smoothing))
        with self._lock:
            self._level = self._level * a + shaped * (1.0 - a)
            self._peak = peak

    @property
    def level(self) -> float:
        with self._lock:
            return self._level

    @property
    def peak(self) -> float:
        with self._lock:
            return self._peak


def _com_initialize() -> bool:
    """Initialize COM on the calling thread (WASAPI capture needs it)."""
    import sys

    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        # COINIT_MULTITHREADED = 0x0; S_OK/S_FALSE mean we own an init.
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
        if hr in (0, 1):
            return True
        if hr == -2147417850:  # RPC_E_CHANGED_MODE - already initialised elsewhere
            return False
        hr = ctypes.windll.ole32.CoInitializeEx(None, 2)  # APARTMENTTHREADED
        return hr in (0, 1)
    except Exception:  # pragma: no cover
        return False


def _com_uninitialize() -> None:
    try:
        import ctypes

        ctypes.windll.ole32.CoUninitialize()
    except Exception:  # pragma: no cover
        pass


def _wasapi_default_output(sd: Any) -> int:
    for api in sd.query_hostapis():
        if "wasapi" in str(api["name"]).lower():
            idx = api.get("default_output_device", -1)
            if idx is not None and idx >= 0:
                return int(idx)
    return int(sd.default.device[1])


def list_audio_devices() -> list[dict[str, Any]]:
    """Return candidate capture devices (outputs are loopback sources)."""
    try:
        import sounddevice as sd
    except ImportError:
        return []
    out: list[dict[str, Any]] = []
    try:
        hostapis = sd.query_hostapis()
        for idx, dev in enumerate(sd.query_devices()):
            api = hostapis[dev["hostapi"]]["name"] if dev.get("hostapi") is not None else "?"
            out.append(
                {
                    "index": idx,
                    "name": dev["name"],
                    "hostapi": api,
                    "inputs": dev.get("max_input_channels", 0),
                    "outputs": dev.get("max_output_channels", 0),
                }
            )
    except Exception as exc:  # pragma: no cover
        log.warning("audio device enumeration failed: %s", exc)
    return out


def list_loopback_speakers() -> list[str]:
    """Speaker names that ``soundcard`` can capture via WASAPI loopback."""
    try:
        import soundcard as sc
    except Exception:
        return []
    try:
        return [str(s.name) for s in sc.all_speakers()]
    except Exception:  # pragma: no cover
        return []
