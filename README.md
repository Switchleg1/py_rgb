# py_rgb

Control your motherboard / system RGB from Python — a full CLI plus an optional
Qt6 (PyQt6) interface, driven by a local `config.toml` in the project root.

Effects include **breathing**, **CPU-usage colouring** and **sound-output
reactive** lighting (real speaker loopback, not the microphone).

```
py_rgb/
├── config.toml          <- local config, lives in the root folder
├── pyproject.toml
├── pyrgb/
│   ├── cli.py           <- argparse CLI (pyrgb ...)
│   ├── daemon.py        <- headless service process (owns the hardware)
│   ├── ipc.py           <- JSON control channel (loopback TCP)
│   ├── service.py       <- logon entry / Scheduled Task / Windows service
│   ├── controller.py    <- local vs remote control surface for the GUI
│   ├── gui.py           <- PyQt6 window + tray icon
│   ├── engine.py        <- frame loop, device targeting
│   ├── effects.py       <- static / breathing / rainbow / cpu / audio / strobe / random / off
│   ├── sources.py       <- CPU sampler (psutil) + audio loopback capture
│   ├── color.py         <- RGB type, parsing, gradients
│   ├── config.py        <- TOML load/save/merge
│   └── backends/        <- msi (native Mystic Light HID) + openrgb + dummy + composite
└── tests/test_pyrgb.py
```

## Install

```bash
pip install -e .          # core (openrgb-python, psutil, hidapi)
pip install PyQt6         # GUI
pip install soundcard sounddevice numpy   # audio-reactive effect
```

## Backends

`pyrgb doctor` tells you exactly what is and isn't working.

| Backend | Covers | Requirements |
|---|---|---|
| `msi` | modern MSI **Mystic Light** onboard zones + JARGB headers (USB `0db0:0076`, e.g. MAG Z890/B860/X870) | `hidapi`, MSI Center's RGB services stopped |
| `openrgb` | everything OpenRGB supports: peripherals, RAM, GPUs, older boards | OpenRGB running with its SDK server |
| `dummy` | virtual devices for previewing without hardware | — |

`backend = "auto"` (default) **combines** every backend that is available into a
single device list, and falls back to `dummy` if nothing is reachable.

### MSI Mystic Light (native)

Newer MSI boards expose a vendor HID controller that OpenRGB does not support.
py_rgb talks to it directly: it drives the controller in *static* mode and
renders the animation in software (exactly how MSI Center implements its own
"CPU temperature" mode).

The 290-byte feature report is **18 zone records of 16 bytes** (record `i` at
offset `1 + 16*i`) plus a trailing save byte:

```
+0      mode       00 off | 01 wave | 02 static | 04 breathing | 05 rainbow
+1..12  palette    four RGB triplets (single-colour modes use the first)
+13     sub        0x03 for palette/rainbow effects, 0x00 otherwise
+14     packed     animate<<7 | direction<<6 | palette<<5 | brightness<<2 | speed
+15     led_count  LEDs on that zone/header

offset 289         save: 0x01 commit to flash, 0x00 volatile
```

Which record maps to which physical zone differs per board, so py_rgb writes
**all 18 records** by default. Writing only the last record (as the original
B860 reverse engineering did) leaves a MAG Z890's zones and JARGB headers dark.
Pin specific zones with `msi.zones = [0, 1]` if you want finer control.

Writes are **volatile by default** (`msi.save_to_flash = false`), so nothing is
committed to the controller's flash and a reboot restores your saved profile.

Stop the vendor services first — they hold the controller and also block
OpenRGB's SMBus access:

```powershell
# elevated PowerShell, or just run start_openrgb.ps1 as Administrator
Stop-Service Mystic_Light_Service, MSI_Case_Service, LightKeeperService -Force
```

Protocol credit: reverse engineered by
[Picachuchu69](https://github.com/Picachuchu69/msi-mystic-light-b860-x870-linux) (MIT).

### OpenRGB

1. Install [OpenRGB](https://openrgb.org/) (`winget install -e --id OpenRGB.OpenRGB`).
2. Run it **as Administrator** so it can use the SMBus.
3. **SDK Server → Enable SDK Server** (port `6742`), or launch `OpenRGB.exe --server`.

`start_openrgb.ps1` in this repo does the service-stopping + elevated launch in
one go.

## CLI

```bash
pyrgb                       # launches the Qt6 GUI
pyrgb devices               # list detected RGB devices
pyrgb doctor                # diagnose backends, audio capture, GUI deps
pyrgb effects               # list effects and their options
pyrgb audio-devices         # list loopback/capture sources

pyrgb set "#ff8800"         # one static colour
pyrgb set orange -b 0.5     # at 50% brightness
pyrgb off                   # everything off

pyrgb run breathing -c "#00aaff" -s 0.4
pyrgb run cpu -o pulse=true            # colour + pulse rate follow CPU load
pyrgb run audio -o gain=1.4            # reacts to your speaker output
pyrgb run rainbow --fps 60 --preview   # --preview prints the live colour
pyrgb run static -d 0 -d "RAM"         # only these devices
pyrgb run breathing --duration 10      # auto-stop
```

Global flags work before or after the subcommand: `--backend {auto,openrgb,msi,dummy}`,
`--config PATH`, `-v`.

## Config file

`config.toml` sits in the repo root (override with `--config` or `$PYRGB_CONFIG`).
Create/reset it with `pyrgb config init`.

```bash
pyrgb config path
pyrgb config show
pyrgb config get general.fps
pyrgb config set general.fps 60
pyrgb config set general.effect cpu
pyrgb config set effects.breathing.color "#ff0055"
```

```toml
[general]
backend = "auto"     # auto | openrgb | msi | dummy
fps = 30
brightness = 1.0     # global 0..1 multiplier
effect = "breathing" # startup effect
devices = []         # empty = all devices; or [0, 1] / ["Aura", "RAM"]
autostart = true     # GUI starts the engine on launch

[msi]
brightness_level = 5   # hardware brightness 1..5
save_to_flash = false  # true = colour survives reboot (wears the flash)
max_hz = 25.0          # cap on HID writes per second
zones = []             # empty = all 18 zone records; or e.g. [0, 1, 2]

[openrgb]
host = "127.0.0.1"
port = 6742
client_name = "py_rgb"
force_direct_mode = true

[effects.breathing]
color = "#00aaff"
speed = 0.5
min_brightness = 0.05

[effects.cpu]
cold = "#00ff66"
warm = "#ffcc00"
hot = "#ff1000"
smoothing = 0.25
pulse = false

[effects.audio]
color = "#00ff9d"
peak_color = "#ff0055"
gain = 1.0
smoothing = 0.35
floor = 0.02

[audio]
device = ""          # blank = auto (default speaker loopback)
samplerate = 48000
blocksize = 512
```

Any missing key falls back to the built-in defaults, so a partial config is fine.

## Effects

| Effect | Description | Options |
|---|---|---|
| `static` | solid colour | `color` |
| `breathing` | smooth sine fade | `color`, `speed`, `min_brightness` |
| `rainbow` | hue cycle / wave across LEDs | `speed`, `spread`, `saturation` |
| `cpu` | colour + bar follow CPU load (cold→warm→hot) | `cold`, `warm`, `hot`, `smoothing`, `pulse` |
| `audio` | reacts to sound coming out of your speakers | `color`, `peak_color`, `gain`, `smoothing`, `floor` |
| `strobe` | hard on/off flashing | `color`, `speed`, `duty` |
| `random` | random colour fades | `speed`, `per_led` |
| `off` | all LEDs off | — |

On multi-LED devices, `cpu` and `audio` render as a bar graph; `rainbow` renders
a travelling wave.

## Audio capture

The `audio` effect listens to your **output**, trying in order:

1. `soundcard` WASAPI loopback of the default speaker (Windows — recommended)
2. `sounddevice` WASAPI loopback (if your PortAudio build supports it)
3. a "Stereo Mix"-style loopback input
4. the default input device (microphone)

`pyrgb audio-devices` shows what is available; pin one with
`pyrgb config set audio.device "Speakers (Realtek Audio)"` (name or PortAudio index).

## GUI

`pyrgb` (or `pyrgb gui`) opens a dark Qt6 window with a live colour preview,
effect picker with auto-generated option controls (colour pickers, sliders,
toggles), brightness/FPS, per-device checkboxes, and save/reload of `config.toml`.

## Daemon / service

py_rgb splits in two:

- **the daemon** owns the LEDs, reads `config.toml`, and accepts commands
- **the Qt6 app** is a client: it writes `config.toml` and tells the daemon to
  re-read it, or sends live commands

Only one process may drive the hardware, so the GUI detects a running daemon and
switches to remote mode automatically (a line under the title bar says which
mode it is in). With no daemon, the GUI drives the hardware itself.

```bash
pyrgb daemon                       # run in the foreground (Ctrl+C to stop)
pyrgb service install              # start at logon
pyrgb service start|stop|status|uninstall

pyrgb ctl status                   # what the daemon is doing right now
pyrgb ctl reload                   # re-read config.toml
pyrgb ctl effect cpu -o pulse=true
pyrgb ctl color "#ff8800"
pyrgb ctl brightness 0.4
pyrgb ctl off | on | stop
```

### Autostart modes

| `--mode` | Mechanism | Admin | Audio effect | Starts before logon |
|---|---|---|---|---|
| `startup` *(default)* | `HKCU\...\Run` | no | ✅ | no |
| `task` | Scheduled Task at logon | yes | ✅ | no |
| `service` | real Windows service (pywin32) | yes | ❌ session 0 | yes |

A session-0 service has no audio endpoint, so the `audio` effect cannot work
there — use `startup`/`task` if you want sound-reactive lighting.

### Control channel

Line-delimited JSON over `127.0.0.1:6743` (configurable, optional shared
`token`). Loopback TCP is used instead of a named pipe because it works across
the session-0 boundary without security-descriptor work.

```toml
[daemon]
host = "127.0.0.1"
port = 6743
token = ""            # optional shared secret
watch_config = true   # auto-reload when config.toml changes on disk
clear_on_exit = false
```

`watch_config` means you can also just edit `config.toml` in an editor and the
daemon picks it up within half a second — no reload command needed.

## Tray icon

```toml
[gui]
tray = true             # show a taskbar tray icon
start_minimized = false # launch straight to the tray
close_to_tray = true    # closing the window hides it instead of quitting
```

The tray icon is drawn live in the **current LED colour**, and its menu has
Show window, an Effect submenu, Pause/Start, All off and Quit.

## Tests

```bash
python tests/test_pyrgb.py      # or: python -m pytest tests
```

They run entirely on the `dummy` backend, so no hardware is required.
