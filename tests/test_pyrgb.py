"""Smoke tests: python -m pytest  (or python tests/test_pyrgb.py)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyrgb.backends.dummy_backend import DummyBackend  # noqa: E402
from pyrgb.color import BLACK, RGB, gradient  # noqa: E402
from pyrgb.config import default_config, dumps_toml, load_config, save_config  # noqa: E402
from pyrgb.effects import EffectContext, REGISTRY, create_effect  # noqa: E402
from pyrgb.engine import Engine  # noqa: E402


def test_color_parsing() -> None:
    assert RGB.parse("#ff8800").to_tuple() == (255, 136, 0)
    assert RGB.parse("f80").to_tuple() == (255, 136, 0)
    assert RGB.parse("255,136,0").to_tuple() == (255, 136, 0)
    assert RGB.parse("orange").to_hex() == "#ff6000"
    assert RGB.from_hsv(0.0, 1.0, 1.0).to_tuple() == (255, 0, 0)
    assert RGB(300, -5, 20).to_tuple() == (255, 0, 20)
    assert RGB(0, 0, 0).blend(RGB(255, 255, 255), 0.5).r == 128
    assert gradient([RGB(0, 0, 0), RGB(255, 255, 255)], 1.0).to_tuple() == (255, 255, 255)


def test_every_effect_renders() -> None:
    ctx = EffectContext(t=1.23, dt=0.03)
    for name in REGISTRY:
        effect = create_effect(name)
        for n in (1, 8, 32):
            colors = effect.render_frame(n, ctx)
            assert len(colors) == n, name
            assert all(isinstance(c, RGB) for c in colors), name


def test_brightness_scaling() -> None:
    effect = create_effect("static", color="#ffffff")
    ctx = EffectContext(brightness=0.5)
    assert effect.render_frame(1, ctx)[0].r == 128
    assert create_effect("off").render_frame(4, EffectContext()) == [BLACK] * 4


def test_cpu_and_audio_effects_follow_signal() -> None:
    class FakeSource:
        def __init__(self, v: float) -> None:
            self.value = v
            self.level = v

    ctx_low = EffectContext(cpu=FakeSource(0.0), audio=FakeSource(0.0))
    ctx_high = EffectContext(cpu=FakeSource(1.0), audio=FakeSource(1.0))

    cpu = create_effect("cpu", smoothing=0.0)
    assert cpu.render_frame(1, ctx_low)[0].to_hex() == "#00ff66"
    assert cpu.render_frame(1, ctx_high)[0].to_hex() == "#ff1000"

    audio = create_effect("audio", smoothing=0.0, gain=1.0)
    quiet = audio.render_frame(1, ctx_low)[0]
    audio.reset()
    loud = audio.render_frame(1, ctx_high)[0]
    assert sum(loud.to_tuple()) > sum(quiet.to_tuple())


def test_engine_runs_on_dummy_backend() -> None:
    backend = DummyBackend()
    backend.connect()
    cfg = default_config()
    cfg["general"]["fps"] = 60
    engine = Engine(backend, cfg, effect=create_effect("rainbow"))
    frames: list[int] = []
    engine.frame_hook = lambda colors: frames.append(len(colors))
    engine.start()
    time.sleep(0.4)
    engine.stop()
    assert engine.frames_rendered > 5
    assert frames and frames[0] == 12          # motherboard LED count
    assert len(backend.frames[1]) == 8          # RAM LED count
    assert engine.error is None


def test_device_targeting() -> None:
    backend = DummyBackend()
    backend.connect()
    cfg = default_config()
    cfg["general"]["devices"] = ["RAM"]
    assert Engine(backend, cfg).targets == [1]
    cfg["general"]["devices"] = [0]
    assert Engine(backend, cfg).targets == [0]


def test_config_roundtrip(tmp_path: Path | None = None) -> None:
    import tempfile

    base = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    path = base / "config.toml"
    cfg = default_config()
    cfg["general"]["fps"] = 77
    cfg["effects"]["breathing"]["color"] = "#123456"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded["general"]["fps"] == 77
    assert loaded["effects"]["breathing"]["color"] == "#123456"
    assert loaded["openrgb"]["port"] == 6742
    assert "[general]" in dumps_toml(cfg)


def test_daemon_control_channel() -> None:
    """Daemon + IPC + RemoteController round trip on the dummy backend."""
    import tempfile

    from pyrgb.controller import RemoteController
    from pyrgb.daemon import Daemon
    from pyrgb.ipc import daemon_status, send_command

    base = Path(tempfile.mkdtemp())
    path = base / "config.toml"
    cfg = default_config()
    cfg["general"]["backend"] = "dummy"
    cfg["general"]["effect"] = "static"
    cfg["general"]["fps"] = 60
    cfg["daemon"]["port"] = 6799
    cfg["daemon"]["watch_config"] = True
    save_config(cfg, path)

    daemon = Daemon(path)
    daemon.start()
    try:
        time.sleep(0.4)
        kw = {"port": 6799}

        assert send_command("ping", **kw)["pong"] is True
        status = daemon_status(port=6799)
        assert status is not None and status["backend"] == "dummy"
        assert status["effect"] == "static"

        reply = send_command("effect", name="breathing", params={"speed": 1.5}, **kw)
        assert reply["ok"] and reply["effect"] == "breathing"
        assert reply["params"]["speed"] == 1.5

        assert send_command("color", value="#123456", **kw)["color"] == "#123456"
        assert send_command("brightness", value=0.5, **kw)["brightness"] == 0.5
        assert send_command("off", **kw)["paused"] is True
        assert send_command("on", **kw)["paused"] is False
        assert send_command("bogus", **kw)["ok"] is False

        # config file edited on disk -> reload command applies it
        cfg["general"]["effect"] = "rainbow"
        cfg["general"]["fps"] = 24
        save_config(cfg, path)
        reply = send_command("reload", **kw)
        assert reply["effect"] == "rainbow" and reply["fps"] == 24

        # the GUI's remote controller drives the same daemon
        remote = RemoteController.probe(cfg)
        assert remote is not None
        remote.set_effect("static", {"color": "#abcdef"})
        assert remote.status()["effect"] == "static"
        assert remote.devices()[0]["name"] == "Virtual Motherboard"
        assert RGB.parse(remote.status()["color"]) is not None
    finally:
        daemon.stop()

    assert daemon_status(port=6799) is None


def test_local_controller() -> None:
    from pyrgb.controller import LocalController

    cfg = default_config()
    cfg["general"]["backend"] = "dummy"
    ctl = LocalController(cfg, "dummy")
    try:
        ctl.set_effect("breathing", {"color": "#ff0000"})
        time.sleep(0.3)
        status = ctl.status()
        assert status["effect"] == "breathing" and status["running"]
        assert len(ctl.devices()) == 2
        ctl.set_brightness(0.25)
        assert ctl.status()["brightness"] == 0.25
        ctl.pause()
        assert not ctl.status()["running"]
    finally:
        ctl.close()


def test_msi_frame_layout() -> None:
    """All 18 zone records must carry the colour (the Z890 fix)."""
    from pyrgb.backends.msi_mystic import (
        MODE_STATIC,
        REPORT_ID,
        REPORT_LEN,
        ZONE_COUNT,
        build_frame,
        zone_offset,
    )

    frame = build_frame([RGB(10, 20, 30)], mode=MODE_STATIC, save=False)
    assert len(frame) == REPORT_LEN and frame[0] == REPORT_ID
    for i in range(ZONE_COUNT):
        off = zone_offset(i)
        assert frame[off] == MODE_STATIC, f"zone {i} mode not set"
        assert tuple(frame[off + 1 : off + 4]) == (10, 20, 30), f"zone {i} colour not set"
    assert frame[289] == 0x00  # volatile: nothing committed to flash

    # a zone subset leaves the other records untouched
    partial = build_frame([RGB(1, 2, 3)], zones=[0], save=True)
    assert tuple(partial[zone_offset(0) + 1 : zone_offset(0) + 4]) == (1, 2, 3)
    assert tuple(partial[zone_offset(5) + 1 : zone_offset(5) + 4]) != (1, 2, 3)
    assert partial[289] == 0x01


def test_cli_commands() -> None:
    from pyrgb.cli import main

    assert main(["--backend", "dummy", "devices"]) == 0
    assert main(["effects"]) == 0
    assert main(["--backend", "dummy", "set", "#102030"]) == 0
    assert main(["--backend", "dummy", "off"]) == 0
    assert main(["--backend", "dummy", "run", "breathing", "--duration", "0.3"]) == 0


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("all good" if not failures else f"{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
