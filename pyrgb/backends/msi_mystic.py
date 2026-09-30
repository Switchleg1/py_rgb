"""MSI Mystic Light backend for the modern ``0db0:0076`` USB controller.

Boards such as the MAG Z890 / B860 / X870 series expose their onboard ARGB
zones and JARGB headers through a vendor-defined HID device that OpenRGB does
not support yet.  The transport is a single HID *feature* report:

    Report ID 0x50, 290 bytes total (index 0 is the report ID)

The payload is **18 zone records of 16 bytes**, record ``i`` at offset
``1 + 16*i``, followed by a single trailing save byte:

    +0      mode       00 off, 01 wave, 02 static, 04 breathing, 05 rainbow
    +1..12  palette    four RGB triplets; single-colour modes use the first
    +13     sub        0x03 for palette/rainbow effects, 0x00 otherwise
    +14     packed     animate<<7 | direction<<6 | palette<<5 | bright<<2 | speed
    +15     led_count  LEDs on that zone/header (left as captured)

    offset 289 (last)  save  0x01 commit to controller flash, 0x00 volatile

Which record drives which physical zone varies by board, so py_rgb writes the
same settings to **every** record by default (``msi.zones = []``).  Writing only
the last record - as the original B860 reverse engineering did - leaves the
onboard zones and JARGB headers of a MAG Z890 untouched.

py_rgb always drives the controller in *static* mode with ``save = 0x00`` and
renders the animation itself, exactly like MSI Center's software "CPU
temperature" mode.  Nothing is written to flash unless you ask for it, so the
lights return to your saved profile after a reboot.

Protocol credit: reverse engineered by Picachuchu69
(github.com/Picachuchu69/msi-mystic-light-b860-x870-linux, MIT).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..color import BLACK, RGB
from .base import BackendError, DeviceInfo, RGBBackend

log = logging.getLogger(__name__)

MSI_VID = 0x0DB0
MSI_PID = 0x0076
REPORT_ID = 0x50
REPORT_LEN = 290

MODE_OFF = 0x00
MODE_WAVE = 0x01
MODE_STATIC = 0x02
MODE_BREATHE = 0x04
MODE_RAINBOW = 0x05

MODE_NAMES = {
    MODE_OFF: "off",
    MODE_WAVE: "wave",
    MODE_STATIC: "static",
    MODE_BREATHE: "breathing",
    MODE_RAINBOW: "rainbow",
}

ZONE_COUNT = 18
ZONE_SIZE = 16
_ZONE_BASE = 1
_SAVE_IDX = 289

# offsets inside one zone record
_R_MODE = 0
_R_COLOR = 1
_R_SUB = 13
_R_PARAM = 14
_R_LEDS = 15


def zone_offset(index: int) -> int:
    return _ZONE_BASE + ZONE_SIZE * index

# 290-byte frame captured from MSI Center sending a static colour.
_TEMPLATE_HEX = """
50 01 00 00 00 00 00 00 00 00 00 00 00 00 03 94
1e 01 00 00 00 00 00 00 00 00 00 00 00 00 03 94
1e 01 00 00 00 00 00 00 00 00 00 00 00 00 03 94
1e 01 00 00 00 00 00 00 00 00 00 00 00 00 03 94
1e 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 01 00 00 00 00 00 00 00 00 00 00 00 00 03 94
ff 01 00 00 00 00 00 00 00 00 00 00 00 00 03 94
ff 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 02 ff 00 00 00 ff 00 00 00 ff ff ff ff 00 b5
ff 00
"""


def _template() -> bytearray:
    buf = bytearray(int(x, 16) for x in _TEMPLATE_HEX.split())
    if len(buf) != REPORT_LEN or buf[0] != REPORT_ID:
        raise BackendError("internal error: bad MSI Mystic Light template")
    return buf


def pack_param(
    brightness: int = 5,
    speed: int = 1,
    direction: int = 0,
    use_palette: int = 1,
    animate: int = 1,
) -> int:
    return (
        ((animate & 1) << 7)
        | ((direction & 1) << 6)
        | ((use_palette & 1) << 5)
        | ((brightness & 7) << 2)
        | (speed & 3)
    )


def build_frame(
    colors: list[RGB],
    mode: int = MODE_STATIC,
    brightness: int = 5,
    speed: int = 1,
    direction: int = 0,
    use_palette: int = 1,
    animate: int = 0,
    sub: int | None = None,
    save: bool = False,
    zones: list[int] | None = None,
) -> bytes:
    """Build one 290-byte feature report.

    ``zones`` selects which of the 18 zone records to write; ``None`` or an
    empty list writes all of them (what actually lights a MAG Z890).
    """
    buf = _template()

    palette = list(colors[:4]) or [BLACK]
    if len(palette) == 1:
        palette = palette * 4
    while len(palette) < 4:
        palette.append(palette[-1])

    targets = zones if zones else range(ZONE_COUNT)
    packed = pack_param(brightness, speed, direction, use_palette, animate)
    sub_value = (0x03 if mode in (MODE_WAVE, MODE_RAINBOW) else 0x00) if sub is None else sub

    for index in targets:
        if not 0 <= index < ZONE_COUNT:
            raise BackendError(f"zone index {index} out of range (0..{ZONE_COUNT - 1})")
        off = zone_offset(index)
        buf[off + _R_MODE] = mode
        for k, c in enumerate(palette[:4]):
            base = off + _R_COLOR + k * 3
            buf[base], buf[base + 1], buf[base + 2] = c.r, c.g, c.b
        buf[off + _R_SUB] = sub_value
        buf[off + _R_PARAM] = packed
        # + _R_LEDS (per-zone LED count) is left exactly as captured

    buf[_SAVE_IDX] = 0x01 if save else 0x00
    return bytes(buf)


class MSIMysticBackend(RGBBackend):
    """Drives the onboard MSI Mystic Light zones over raw HID."""

    name = "msi"

    def __init__(
        self,
        brightness_level: int = 5,
        save_to_flash: bool = False,
        max_hz: float = 25.0,
        zones: list[int] | None = None,
        vendor_id: int = MSI_VID,
        product_id: int = MSI_PID,
    ) -> None:
        self.brightness_level = max(1, min(5, int(brightness_level)))
        self.save_to_flash = bool(save_to_flash)
        self.zones = [int(z) for z in zones] if zones else []
        self.min_interval = 1.0 / max(1.0, float(max_hz))
        self.vendor_id = vendor_id
        self.product_id = product_id
        self._dev: Any = None
        self._info: DeviceInfo | None = None
        self._lock = threading.Lock()
        self._last_sent: tuple[int, int, int] | None = None
        self._last_time = 0.0
        self.product_name = "MSI Mystic Light"

    # ------------------------------------------------------------------
    @staticmethod
    def find() -> dict[str, Any] | None:
        """Return the HID entry for the Mystic Light controller, if present."""
        try:
            import hid
        except ImportError:
            return None
        try:
            for entry in hid.enumerate(MSI_VID, MSI_PID):
                return entry
        except Exception:  # pragma: no cover
            return None
        return None

    def connect(self) -> None:
        try:
            import hid
        except ImportError as exc:
            raise BackendError("hidapi is not installed (pip install hidapi)") from exc

        entry = self.find()
        if entry is None:
            raise BackendError(
                f"no MSI Mystic Light controller found ({self.vendor_id:04x}:{self.product_id:04x})"
            )

        dev = hid.device()
        try:
            dev.open_path(entry["path"])
        except Exception as exc:
            raise BackendError(
                f"cannot open the Mystic Light HID device: {exc} "
                "(close MSI Center / stop Mystic_Light_Service, then retry)"
            ) from exc

        self._dev = dev
        self.product_name = str(entry.get("product_string") or "MSI Mystic Light")
        zone_list = (
            [f"zone {z}" for z in self.zones]
            if self.zones
            else [f"all {ZONE_COUNT} zones (onboard + JARGB)"]
        )
        self._info = DeviceInfo(
            index=0,
            name=f"{entry.get('manufacturer_string', 'MSI')} {self.product_name}",
            led_count=1,
            kind="motherboard",
            zones=zone_list,
        )

    def devices(self) -> list[DeviceInfo]:
        return [self._info] if self._info else []

    # ------------------------------------------------------------------
    def set_colors(self, device_index: int, colors: list[RGB]) -> None:
        if device_index != 0:
            raise BackendError(f"no device with index {device_index}")
        color = colors[0] if colors else BLACK
        self._send_static(color)

    def _send_static(self, color: RGB) -> None:
        key = color.to_tuple()
        now = time.perf_counter()
        with self._lock:
            if key == self._last_sent and not self.save_to_flash:
                return
            if now - self._last_time < self.min_interval and key != self._last_sent:
                # keep the controller from being flooded; drop this frame
                if now - self._last_time < self.min_interval * 0.5:
                    return
            mode = MODE_OFF if key == (0, 0, 0) else MODE_STATIC
            frame = build_frame(
                [color],
                mode=mode,
                brightness=self.brightness_level,
                animate=0,
                save=self.save_to_flash,
                zones=self.zones,
            )
            self._write(frame)
            self._last_sent = key
            self._last_time = now

    def _write(self, frame: bytes) -> None:
        if self._dev is None:
            raise BackendError("backend not connected")
        try:
            written = self._dev.send_feature_report(frame)
        except Exception as exc:
            raise BackendError(f"Mystic Light HID write failed: {exc}") from exc
        if written is not None and written < 0:
            raise BackendError("Mystic Light HID write rejected by the device")

    # ------------------------------------------------------------------
    def apply_hardware_effect(
        self,
        mode: int,
        colors: list[RGB] | None = None,
        speed: int = 1,
        direction: int = 0,
        brightness: int | None = None,
        save: bool | None = None,
    ) -> None:
        """Hand an effect to the controller firmware (keeps running standalone)."""
        frame = build_frame(
            colors or [RGB(0, 170, 255)],
            mode=mode,
            brightness=self.brightness_level if brightness is None else brightness,
            speed=speed,
            direction=direction,
            use_palette=0 if mode == MODE_RAINBOW else 1,
            animate=0 if mode in (MODE_OFF, MODE_STATIC) else 1,
            save=self.save_to_flash if save is None else save,
            zones=self.zones,
        )
        with self._lock:
            self._write(frame)
            self._last_sent = None

    def read_state(self) -> dict[str, Any] | None:
        """Read the controller back to verify it accepted our last frame."""
        if self._dev is None:
            return None
        try:
            with self._lock:
                buf = self._dev.get_feature_report(REPORT_ID, REPORT_LEN)
        except Exception:
            return None
        if not buf or len(buf) < REPORT_LEN:
            return None
        off = zone_offset(self.zones[0] if self.zones else 0)
        param = buf[off + _R_PARAM]
        mode = buf[off + _R_MODE]
        base = off + _R_COLOR
        return {
            "zone": self.zones[0] if self.zones else 0,
            "mode": mode,
            "mode_name": MODE_NAMES.get(mode, "unknown"),
            "color": RGB(buf[base], buf[base + 1], buf[base + 2]),
            "speed": param & 0b11,
            "brightness": (param >> 2) & 0b111,
            "animated": bool(param & 0x80),
            "saved": bool(buf[_SAVE_IDX]),
        }

    def close(self) -> None:
        dev, self._dev = self._dev, None
        if dev is not None:
            try:
                dev.close()
            except Exception:  # pragma: no cover
                pass
