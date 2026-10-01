"""Windows service / autostart integration for the py_rgb daemon.

Two install modes:

``service``  a real Windows service via pywin32.  Starts before logon and
             survives logoff, but runs in session 0 - no audio loopback there.

``task``     a Scheduled Task that runs at logon in your interactive session.
             Keeps every feature (including audio-reactive effects) and needs
             no elevation.  This is the default.

Both run exactly the same thing: ``pyrgb daemon`` reading ``config.toml``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .config import ROOT_DIR, config_path

log = logging.getLogger(__name__)

SERVICE_NAME = "py_rgb"
SERVICE_DISPLAY_NAME = "py_rgb LED daemon"
SERVICE_DESCRIPTION = "Drives motherboard/system RGB lighting from config.toml."
TASK_NAME = "py_rgb daemon"


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

def is_frozen() -> bool:
    """True when running from a PyInstaller build."""
    return bool(getattr(sys, "frozen", False))


def python_exe() -> str:
    """Prefer pythonw.exe so no console window flashes on logon."""
    exe = Path(sys.executable)
    windowed = exe.with_name("pythonw.exe")
    return str(windowed if windowed.is_file() else exe)


def _windowless_exe() -> str:
    """The console-free executable to launch background/UI processes with."""
    exe = Path(sys.executable)
    for candidate in ("pyrgbw.exe", "pyrgb-daemon.exe"):
        sibling = exe.with_name(candidate)
        if sibling.is_file():
            return str(sibling)
    return str(exe)


def _command(subcommand: str, cfg_path: Path | None = None) -> list[str]:
    path = cfg_path or config_path()
    if is_frozen():
        return [_windowless_exe(), subcommand, "--config", str(path)]
    return [python_exe(), "-m", "pyrgb", subcommand, "--config", str(path)]


def daemon_command(cfg_path: Path | None = None) -> list[str]:
    """Command line that launches the headless daemon only."""
    return _command("daemon", cfg_path)


def tray_command(cfg_path: Path | None = None) -> list[str]:
    """Command line that launches the daemon *and* the tray icon."""
    return _command("tray", cfg_path)


def startup_command(cfg_path: Path | None = None, with_tray: bool = False) -> list[str]:
    return tray_command(cfg_path) if with_tray else daemon_command(cfg_path)


def _run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(args, capture_output=True, text=True, shell=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# ---------------------------------------------------------------------
# Scheduled Task backend (recommended: runs in the user session)
# ---------------------------------------------------------------------

def task_install(cfg_path: Path | None = None, delay_seconds: int = 15) -> tuple[bool, str]:
    cmd = daemon_command(cfg_path)
    quoted = f'"{cmd[0]}" ' + " ".join(
        f'"{part}"' if " " in part else part for part in cmd[1:]
    )
    base = [
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAME,
        "/SC", "ONLOGON",
        "/RL", "LIMITED",
        "/TR", quoted,
    ]
    delay = f"{delay_seconds // 3600:04d}:{(delay_seconds % 3600) // 60:02d}"
    code, out = _run(base + ["/DELAY", delay])
    if code != 0:
        # /DELAY is rejected on some builds - retry without it
        code, out = _run(base)
    return code == 0, out or f"scheduled task '{TASK_NAME}' installed"


def task_uninstall() -> tuple[bool, str]:
    code, out = _run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    return code == 0, out


def task_start() -> tuple[bool, str]:
    code, out = _run(["schtasks", "/Run", "/TN", TASK_NAME])
    return code == 0, out


def task_stop() -> tuple[bool, str]:
    code, out = _run(["schtasks", "/End", "/TN", TASK_NAME])
    return code == 0, out


def task_installed() -> bool:
    code, _out = _run(["schtasks", "/Query", "/TN", TASK_NAME])
    return code == 0


# ---------------------------------------------------------------------
# Registry Run key (no elevation required)
# ---------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "py_rgb"


def _quote(cmd: list[str]) -> str:
    return f'"{cmd[0]}" ' + " ".join(f'"{p}"' if " " in p else p for p in cmd[1:])


def _run_command_string(cfg_path: Path | None = None, with_tray: bool = False) -> str:
    return _quote(startup_command(cfg_path, with_tray))


def startup_install(cfg_path: Path | None = None, with_tray: bool = False) -> tuple[bool, str]:
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(
                key, RUN_VALUE, 0, winreg.REG_SZ, _run_command_string(cfg_path, with_tray)
            )
        what = "daemon + tray icon" if with_tray else "daemon only"
        return True, (
            f"registered HKCU\\{RUN_KEY}\\{RUN_VALUE} "
            f"({what}, starts at logon, no admin needed)"
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def startup_entry() -> str | None:
    """The currently registered logon command, if any."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, RUN_VALUE)
        return str(value)
    except Exception:
        return None


def startup_has_tray() -> bool:
    entry = startup_entry()
    return bool(entry and " tray" in entry)


def startup_uninstall() -> tuple[bool, str]:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
        return True, "logon entry removed"
    except FileNotFoundError:
        return True, "logon entry was not present"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def startup_installed() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except Exception:
        return False


def daemon_spawn(
    cfg_path: Path | None = None, wait: float = 0.0, host: str = "127.0.0.1", port: int = 6743
) -> tuple[bool, str]:
    """Start the daemon detached from this console.

    With ``wait`` > 0, block until the control channel answers (or time out).
    """
    import time

    from .ipc import is_running

    if is_running(host, port):
        return True, "daemon already running"
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    try:
        subprocess.Popen(  # noqa: S603
            daemon_command(cfg_path),
            creationflags=flags,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    if wait > 0:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if is_running(host, port):
                return True, "daemon started"
            time.sleep(0.2)
        return False, f"daemon did not answer within {wait:.0f}s"
    return True, "daemon started"


def daemon_kill() -> tuple[bool, str]:
    from .ipc import send_command

    try:
        send_command("shutdown")
    except ConnectionError as exc:
        return False, str(exc)
    return True, "daemon stopping"


# ---------------------------------------------------------------------
# Windows service backend (pywin32)
# ---------------------------------------------------------------------

def _service_module_path() -> str:
    return str(Path(__file__).resolve())


def service_available() -> bool:
    try:
        import win32serviceutil  # noqa: F401

        return True
    except ImportError:
        return False


def service_control(action: str, cfg_path: Path | None = None) -> tuple[bool, str]:
    """install / remove / start / stop / status the pywin32 service."""
    if not service_available():
        return False, "pywin32 is not installed (pip install pywin32)"

    if action == "install":
        args = [
            sys.executable, _service_module_path(),
            "--startup", "auto", "install",
        ]
        code, out = _run(args)
        if code == 0 and cfg_path:
            _write_service_config(cfg_path)
        return code == 0, out or "service installed"
    if action in ("remove", "uninstall"):
        code, out = _run([sys.executable, _service_module_path(), "remove"])
        return code == 0, out or "service removed"
    if action in ("start", "stop", "restart"):
        code, out = _run([sys.executable, _service_module_path(), action])
        return code == 0, out or f"service {action}ed"
    if action == "status":
        code, out = _run(["sc", "query", SERVICE_NAME])
        return code == 0, out
    return False, f"unknown service action {action!r}"


def _write_service_config(cfg_path: Path) -> None:
    """Remember which config the service should read (registry parameter)."""
    try:
        import winreg

        key = winreg.CreateKey(
            winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"
        )
        with key:
            winreg.SetValueEx(key, "PyRgbConfig", 0, winreg.REG_SZ, str(cfg_path))
    except Exception as exc:  # pragma: no cover - needs admin
        log.warning("could not record the config path for the service: %s", exc)


def _service_config_path() -> Path:
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"
        )
        with key:
            value, _kind = winreg.QueryValueEx(key, "PyRgbConfig")
            return Path(str(value))
    except Exception:
        return ROOT_DIR / "config.toml"


if os.name == "nt" and service_available():  # pragma: no cover - service runtime
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    from .daemon import Daemon

    class PyRgbService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args: list[str]) -> None:
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.daemon: Daemon | None = None

        def SvcStop(self) -> None:  # noqa: N802
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.daemon is not None:
                self.daemon.stop()
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self) -> None:  # noqa: N802
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            path = _service_config_path()
            logging.basicConfig(
                filename=str(path.parent / "pyrgb-service.log"),
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            )
            try:
                self.daemon = Daemon(path)
                self.daemon.start()
            except Exception as exc:  # noqa: BLE001
                servicemanager.LogErrorMsg(f"py_rgb daemon failed to start: {exc}")
                logging.exception("daemon failed to start")
                return
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)

    def _service_main() -> None:
        if len(sys.argv) == 1:
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(PyRgbService)
            servicemanager.StartServiceCtrlDispatcher()
        else:
            win32serviceutil.HandleCommandLine(PyRgbService)

    if __name__ == "__main__":
        _service_main()
