"""PyQt6 interface for py_rgb.

The window is a *client*: when the daemon/service is running it edits
``config.toml`` and sends it commands; otherwise it drives the hardware itself.
It can live in the taskbar tray when ``[gui] tray = true``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStatusBar,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .backends import BackendError
from .color import BLACK, RGB
from .config import config_path, load_config
from .controller import Controller, LocalController, RemoteController, make_controller
from .effects import REGISTRY, create_effect, effect_names


log = logging.getLogger(__name__)

#: readable secondary text on the dark palette (palette(mid) is far too dim)
HINT_STYLE = "color: #a8b0bd;"
#: live sensor readout - brighter still, it changes every frame
READOUT_STYLE = "color: #6fd3ff; font-weight: 600;"


def make_color_icon(color: RGB, size: int = 32) -> QIcon:
    """Tray icon that mirrors the current LED colour."""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color.r, color.g, color.b))
    painter.setPen(QColor(20, 20, 20))
    painter.drawEllipse(2, 2, size - 4, size - 4)
    painter.end()
    return QIcon(pixmap)


class ColorSwatch(QWidget):
    """Click-to-pick colour button with a live preview."""

    changed = pyqtSignal(str)

    def __init__(self, color: str = "#00aaff", clickable: bool = True) -> None:
        super().__init__()
        self._color = RGB.parse(color)
        self._clickable = clickable
        self.setMinimumSize(48, 26)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor
        )

    def color(self) -> RGB:
        return self._color

    def set_color(self, color: RGB | str) -> None:
        self._color = RGB.parse(color)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802, ANN001
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(self._color.r, self._color.g, self._color.b))
        painter.setPen(QColor(70, 70, 70))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 5, 5)
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802, ANN001
        if not self._clickable:
            return
        picked = QColorDialog.getColor(
            QColor(self._color.r, self._color.g, self._color.b), self, "Pick a color"
        )
        if picked.isValid():
            self.set_color(RGB(picked.red(), picked.green(), picked.blue()))
            self.changed.emit(self._color.to_hex())


class MainWindow(QMainWindow):
    def __init__(self, cfg: dict[str, Any], backend_name: str | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.backend_name = backend_name
        self.setWindowTitle("py_rgb")
        self.resize(560, 640)

        self.controller: Controller = make_controller(cfg, backend_name)
        self._option_widgets: dict[str, QWidget] = {}
        self._updating = False
        self._last_color = BLACK

        self._build_ui()
        self.tray: QSystemTrayIcon | None = None
        self._build_tray()
        self._load_from_state()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(400)

        if isinstance(self.controller, LocalController) and cfg.get("general", {}).get(
            "autostart", True
        ):
            self.controller.resume()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        root.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_effect_tab(), "Effect")
        self.tabs.addTab(self._build_devices_tab(), "Devices")
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        root.addWidget(self.tabs, 1)

        root.addLayout(self._build_action_bar())

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._populate_devices()

    # -- header: always-visible preview + mode -------------------------
    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(header)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        top = QHBoxLayout()
        self.preview = ColorSwatch("#000000", clickable=False)
        self.preview.setMinimumHeight(34)
        self.preview.setMinimumWidth(120)
        self.preview_label = QLabel("#000000")
        self.preview_label.setStyleSheet(READOUT_STYLE)
        self.live_label = QLabel("")
        self.live_label.setStyleSheet(READOUT_STYLE)
        self.live_label.setMinimumWidth(1)
        top.addWidget(self.preview, 1)
        top.addWidget(self.preview_label)
        layout.addLayout(top)
        layout.addWidget(self.live_label)

        self.mode_label = QLabel()
        self.mode_label.setStyleSheet(HINT_STYLE)
        self.mode_label.setMinimumWidth(1)
        layout.addWidget(self.mode_label)
        return header

    # -- tab 1: effect --------------------------------------------------
    def _build_effect_tab(self) -> QWidget:
        page = QWidget()
        ev = QVBoxLayout(page)
        ev.setContentsMargins(12, 12, 12, 12)
        ev.setSpacing(8)

        row = QHBoxLayout()
        self.effect_combo = QComboBox()
        for name in effect_names():
            self.effect_combo.addItem(name, name)
        self.effect_combo.currentIndexChanged.connect(self._on_effect_changed)
        row.addWidget(QLabel("Effect:"))
        row.addWidget(self.effect_combo, 1)
        ev.addLayout(row)

        self.effect_help = QLabel("")
        self.effect_help.setWordWrap(True)
        self.effect_help.setStyleSheet(HINT_STYLE)
        self.effect_help.setMinimumWidth(1)
        ev.addWidget(self.effect_help)

        options_box = QGroupBox("Options")
        ob = QVBoxLayout(options_box)
        ob.setContentsMargins(10, 8, 10, 8)
        self.options_widget = QWidget()
        self.options_form = QFormLayout(self.options_widget)
        self.options_form.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(230)
        scroll.setWidget(self.options_widget)
        ob.addWidget(scroll)
        ev.addWidget(options_box, 1)

        global_box = QGroupBox("Output")
        gf = QFormLayout(global_box)
        self.brightness = QSlider(Qt.Orientation.Horizontal)
        self.brightness.setRange(0, 100)
        self.brightness.setValue(100)
        self.brightness.valueChanged.connect(self._on_brightness)
        self.brightness_label = QLabel("100%")
        brow = QHBoxLayout()
        brow.addWidget(self.brightness, 1)
        brow.addWidget(self.brightness_label)
        bw = QWidget()
        bw.setLayout(brow)
        gf.addRow("Brightness", bw)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 144)
        self.fps_spin.setValue(30)
        self.fps_spin.valueChanged.connect(self._on_fps)
        gf.addRow("Frame rate", self.fps_spin)
        ev.addWidget(global_box)
        return page

    # -- tab 2: devices -------------------------------------------------
    def _build_devices_tab(self) -> QWidget:
        page = QWidget()
        dv = QVBoxLayout(page)
        dv.setContentsMargins(12, 12, 12, 12)
        dv.setSpacing(8)

        hint = QLabel("Tick the devices the effect should drive.")
        hint.setStyleSheet(HINT_STYLE)
        dv.addWidget(hint)

        self.device_list = QListWidget()
        self.device_list.setMinimumWidth(1)
        self.device_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.device_list.itemChanged.connect(self._on_devices_changed)
        dv.addWidget(self.device_list, 1)

        self.backend_label = QLabel("")
        self.backend_label.setWordWrap(True)
        self.backend_label.setStyleSheet(HINT_STYLE)
        self.backend_label.setMinimumWidth(1)
        dv.addWidget(self.backend_label)

        refresh = QPushButton("Rescan devices")
        refresh.clicked.connect(self._populate_devices)
        dv.addWidget(refresh, 0, Qt.AlignmentFlag.AlignLeft)
        return page

    # -- tab 3: settings ------------------------------------------------
    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        startup_box = QGroupBox("Startup")
        sf = QFormLayout(startup_box)
        self.autostart_check = QCheckBox("Start py_rgb with Windows")
        self.autostart_check.setToolTip(
            "Adds HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\py_rgb.\n"
            "Off by default; no administrator rights required."
        )
        self.autostart_check.toggled.connect(self._on_autostart_toggled)
        sf.addRow(self.autostart_check)

        self.autostart_mode = QComboBox()
        self.autostart_mode.addItem("Daemon + tray icon", True)
        self.autostart_mode.addItem("Daemon only (no tray)", False)
        self.autostart_mode.setToolTip(
            "The daemon drives the LEDs with no window.\n"
            "Adding the tray icon also gives you quick controls in the taskbar."
        )
        self.autostart_mode.currentIndexChanged.connect(self._on_autostart_mode_changed)
        sf.addRow("Launch at logon", self.autostart_mode)

        self.tray_check = QCheckBox("Minimise to tray instead of quitting")
        self.tray_check.toggled.connect(
            lambda checked: self.cfg.setdefault("gui", {}).__setitem__("close_to_tray", checked)
        )
        sf.addRow(self.tray_check)

        self.minimized_check = QCheckBox("Start minimised to tray")
        self.minimized_check.toggled.connect(
            lambda checked: self.cfg.setdefault("gui", {}).__setitem__(
                "start_minimized", checked
            )
        )
        sf.addRow(self.minimized_check)

        self.autostart_hint = QLabel("")
        self.autostart_hint.setWordWrap(True)
        self.autostart_hint.setStyleSheet(HINT_STYLE)
        self.autostart_hint.setMinimumWidth(1)
        sf.addRow(self.autostart_hint)
        outer.addWidget(startup_box)

        config_box = QGroupBox("Configuration file")
        cf = QVBoxLayout(config_box)
        self.config_label = QLabel(str(config_path()))
        self.config_label.setWordWrap(True)
        self.config_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.config_label.setStyleSheet(HINT_STYLE)
        self.config_label.setMinimumWidth(1)
        cf.addWidget(self.config_label)

        crow = QHBoxLayout()
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(self._open_config_folder)
        crow.addWidget(open_btn)
        crow.addStretch(1)
        cf.addLayout(crow)
        outer.addWidget(config_box)

        outer.addStretch(1)
        return page

    # -- persistent action bar ------------------------------------------
    def _build_action_bar(self) -> QHBoxLayout:
        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self._toggle)
        self.off_btn = QPushButton("All off")
        self.off_btn.clicked.connect(self._all_off)
        self.save_btn = QPushButton("Save && apply")
        self.save_btn.clicked.connect(self._save_config)
        self.reload_btn = QPushButton("Reload config")
        self.reload_btn.clicked.connect(self._reload_config)
        for b in (self.start_btn, self.off_btn, self.save_btn, self.reload_btn):
            buttons.addWidget(b)
        return buttons

    def _open_config_folder(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(config_path().parent)))

    def _build_tray(self) -> bool:
        """Create the tray icon. Returns False when the tray is not (yet) there.

        At logon Explorer often has not created the notification area yet, so the
        caller retries for a while instead of silently giving up.
        """
        gui_cfg = self.cfg.get("gui", {})
        if getattr(self, "tray", None) is not None:
            return True
        self.tray: QSystemTrayIcon | None = None
        if not gui_cfg.get("tray", True):
            return True  # tray disabled on purpose - nothing to wait for
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False

        self.tray = QSystemTrayIcon(make_color_icon(BLACK), self)
        self.tray.setToolTip("py_rgb")
        menu = QMenu()

        self.act_show = QAction("Show window", self)
        self.act_show.triggered.connect(self._show_window)
        menu.addAction(self.act_show)
        menu.addSeparator()

        effects_menu = menu.addMenu("Effect")
        for name in effect_names():
            action = QAction(name, self)
            action.triggered.connect(lambda _checked=False, n=name: self._tray_effect(n))
            effects_menu.addAction(action)

        self.act_toggle = QAction("Pause", self)
        self.act_toggle.triggered.connect(self._toggle)
        menu.addAction(self.act_toggle)

        act_off = QAction("All off", self)
        act_off.triggered.connect(self._all_off)
        menu.addAction(act_off)
        menu.addSeparator()

        act_quit = QAction("Quit py_rgb", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()
        self._apply_preview(self._last_color)
        log.info("tray icon created")
        return True

    def start_tray_retry(self, attempts: int = 30, interval_ms: int = 2000) -> None:
        """Keep trying to create the tray icon (logon race), then give up visibly."""
        if self._build_tray():
            return
        self._tray_attempts = attempts
        self._tray_timer = QTimer(self)

        def attempt() -> None:
            self._tray_attempts -= 1
            if self._build_tray():
                self._tray_timer.stop()
                log.info("tray icon appeared after the notification area was ready")
                return
            if self._tray_attempts <= 0:
                self._tray_timer.stop()
                log.warning("system tray never became available; showing the window instead")
                self.show()
                self.raise_()

        self._tray_timer.timeout.connect(attempt)
        self._tray_timer.start(interval_ms)
        log.info("system tray not ready yet; retrying for %.0fs", attempts * interval_ms / 1000)

    # ------------------------------------------------------------------
    # state sync
    # ------------------------------------------------------------------
    def _populate_devices(self) -> None:
        self._updating = True
        self.device_list.clear()
        devices = self.controller.devices()
        selected = self.controller.status().get("devices") or []
        for dev in devices:
            item = QListWidgetItem(
                f"[{dev['index']}] {dev['name']} - {dev['leds']} LEDs ({dev['kind']})"
            )
            item.setData(Qt.ItemDataRole.UserRole, dev["index"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = (not selected) or dev["index"] in selected
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            self.device_list.addItem(item)
        if not devices:
            self.device_list.addItem("no devices detected")
        if hasattr(self, "backend_label"):
            self.backend_label.setText(
                f"Backend: {self.controller.backend_name}  -  "
                f"{len(devices)} device(s) detected"
            )
        self._updating = False

    def _load_from_state(self) -> None:
        status = self.controller.status()
        self._updating = True
        name = status.get("effect") or self.cfg.get("general", {}).get("effect", "breathing")
        idx = self.effect_combo.findData(name)
        self.effect_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.brightness.setValue(int(float(status.get("brightness", 1.0)) * 100))
        self.brightness_label.setText(f"{self.brightness.value()}%")
        self.fps_spin.setValue(int(status.get("fps", 30) or 30))
        gui_cfg = self.cfg.get("gui", {})
        self.tray_check.setChecked(bool(gui_cfg.get("close_to_tray", True)))
        self.minimized_check.setChecked(bool(gui_cfg.get("start_minimized", False)))
        self._sync_autostart_checkbox()
        self._updating = False
        self._rebuild_options(status.get("params") or {})
        self._update_mode_label()

    def _update_mode_label(self) -> None:
        if isinstance(self.controller, RemoteController):
            self.mode_label.setText("Mode: connected to the py_rgb daemon")
            self.mode_label.setToolTip(
                "The background daemon owns the LEDs.\n"
                "Changes are sent to it and saved to config.toml."
            )
        else:
            self.mode_label.setText("Mode: running locally (no daemon)")
            self.mode_label.setToolTip("This window owns the hardware directly.")

    def _rebuild_options(self, params: dict[str, Any] | None = None) -> None:
        while self.options_form.rowCount():
            self.options_form.removeRow(0)
        self._option_widgets.clear()

        name = self.effect_combo.currentData() or "breathing"
        cls = REGISTRY[name]
        params = params or dict(create_effect(name, self.cfg).params)
        self.effect_help.setText(cls.description + self._source_hint(name))

        self._updating = True
        for key, (kind, default, lo, hi) in cls.options.items():
            value = params.get(key, default)
            if kind == "choice":
                w: QWidget = QComboBox()
                for choice in lo or []:
                    w.addItem(str(choice), str(choice))
                idx = w.findData(str(value))
                w.setCurrentIndex(idx if idx >= 0 else 0)
                w.currentIndexChanged.connect(
                    lambda _i, k=key, combo=w: self._set_param(k, combo.currentData())
                )
            elif kind == "color":
                w = ColorSwatch(str(value))
                w.changed.connect(lambda hexval, k=key: self._set_param(k, hexval))
            elif kind == "bool":
                w = QCheckBox()
                w.setChecked(_as_bool(value))
                w.toggled.connect(lambda checked, k=key: self._set_param(k, checked))
            elif kind == "int":
                w = QSpinBox()
                w.setRange(int(lo if lo is not None else 0), int(hi if hi is not None else 1000))
                w.setValue(int(value))
                w.valueChanged.connect(lambda v, k=key: self._set_param(k, v))
            else:
                w = QDoubleSpinBox()
                w.setDecimals(2)
                w.setSingleStep(0.05)
                w.setRange(
                    float(lo if lo is not None else 0.0), float(hi if hi is not None else 100.0)
                )
                w.setValue(float(value))
                w.valueChanged.connect(lambda v, k=key: self._set_param(k, v))
            self._option_widgets[key] = w
            self.options_form.addRow(key.replace("_", " ").title(), w)
        self._updating = False

    def _source_hint(self, name: str) -> str:
        if name == "cpu":
            status = self.controller.status()
            provider = status.get("temp_provider", "none")
            if status.get("temp_available"):
                return f"  (temperature via {provider}; 'load' uses psutil CPU usage)"
            return "  (no temperature sensor - run MSI Afterburner, or use source = load)"
        if name == "audio":
            return "  (captures speaker output; not available to a session-0 service)"
        return ""

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _set_param(self, key: str, value: Any) -> None:
        if self._updating:
            return
        self.controller.set_param(key, value)
        effects = self.cfg.setdefault("effects", {})
        effects.setdefault(self.effect_combo.currentData(), {})[key] = value

    def _on_effect_changed(self) -> None:
        if self._updating:
            return
        name = self.effect_combo.currentData()
        if not name:
            return
        params = dict(self.cfg.get("effects", {}).get(name, {}) or {})
        self.controller.set_effect(name, params)
        self.cfg.setdefault("general", {})["effect"] = name
        self._rebuild_options(dict(create_effect(name, self.cfg).params))

    def _on_brightness(self, value: int) -> None:
        self.brightness_label.setText(f"{value}%")
        if self._updating:
            return
        self.controller.set_brightness(value / 100.0)
        self.cfg.setdefault("general", {})["brightness"] = round(value / 100.0, 3)

    def _on_fps(self, value: int) -> None:
        if self._updating:
            return
        self.controller.set_fps(value)
        self.cfg.setdefault("general", {})["fps"] = value

    def _on_devices_changed(self) -> None:
        if self._updating:
            return
        indices = []
        for i in range(self.device_list.count()):
            item = self.device_list.item(i)
            idx = item.data(Qt.ItemDataRole.UserRole)
            if idx is not None and item.checkState() == Qt.CheckState.Checked:
                indices.append(int(idx))
        self.controller.set_devices(indices)
        self.cfg.setdefault("general", {})["devices"] = indices

    # -- windows autostart ---------------------------------------------
    def _sync_autostart_checkbox(self) -> None:
        from . import service as svc

        was = self._updating
        self._updating = True
        try:
            installed = svc.startup_installed()
            self.autostart_check.setChecked(installed)
            if installed:
                with_tray = svc.startup_has_tray()
                self.autostart_mode.setCurrentIndex(0 if with_tray else 1)
                self.autostart_hint.setText(f"Runs: {svc.startup_entry()}")
            else:
                self.autostart_hint.setText("Not registered - py_rgb will not start with Windows.")
            self.autostart_mode.setEnabled(installed)
        finally:
            self._updating = was

    def _on_autostart_toggled(self, checked: bool) -> None:
        if self._updating:
            return
        from . import service as svc

        if checked:
            ok, message = svc.startup_install(with_tray=self._autostart_wants_tray())
        else:
            ok, message = svc.startup_uninstall()
        if not ok:
            QMessageBox.warning(self, "py_rgb", f"Could not change the startup entry:\n{message}")
        else:
            self.statusBar().showMessage(message, 5000)
        self._sync_autostart_checkbox()

    def _autostart_wants_tray(self) -> bool:
        data = self.autostart_mode.currentData()
        return True if data is None else bool(data)

    def _on_autostart_mode_changed(self) -> None:
        if self._updating:
            return
        from . import service as svc

        if not svc.startup_installed():
            return
        ok, message = svc.startup_install(with_tray=self._autostart_wants_tray())
        if not ok:
            QMessageBox.warning(self, "py_rgb", f"Could not update the startup entry:\n{message}")
        else:
            self.statusBar().showMessage(message, 5000)
        self._sync_autostart_checkbox()

    def _toggle(self) -> None:
        running = bool(self.controller.status().get("running"))
        if running:
            self.controller.pause()
        else:
            self.controller.resume()

    def _all_off(self) -> None:
        try:
            self.controller.all_off()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "py_rgb", str(exc))
        self._apply_preview(BLACK)

    def _save_config(self) -> None:
        try:
            path = self.controller.apply_config(self.cfg)
            msg = f"saved {path}"
            if isinstance(self.controller, RemoteController):
                msg += " and told the daemon to reload"
            self.statusBar().showMessage(msg, 4000)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "py_rgb", f"Could not save config:\n{exc}")

    def _reload_config(self) -> None:
        self.cfg = load_config()
        if isinstance(self.controller, LocalController):
            self.controller.cfg = self.cfg
        self._load_from_state()
        self.statusBar().showMessage(f"reloaded {config_path()}", 3000)

    # -- tray ----------------------------------------------------------
    def _tray_effect(self, name: str) -> None:
        idx = self.effect_combo.findData(name)
        if idx >= 0:
            self.effect_combo.setCurrentIndex(idx)

    def _tray_activated(self, reason) -> None:  # noqa: ANN001
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_window() if not self.isVisible() else self.hide()

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        self._force_quit = True
        if self.tray is not None:
            self.tray.hide()
        self.close()
        QApplication.quit()

    # ------------------------------------------------------------------
    # periodic refresh
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        status = self.controller.status()

        color = RGB.parse(status.get("color") or "#000000")
        if color != self._last_color:
            self._apply_preview(color)

        running = bool(status.get("running"))
        self.start_btn.setText("Pause" if running else "Start")
        if self.tray is not None:
            self.act_toggle.setText("Pause" if running else "Start")

        self._update_live_readout(status)

        parts = [f"backend: {self.controller.backend_name}"]
        parts.append(f"{status.get('fps', 0)} fps" if running else "stopped")
        effect = status.get("effect")
        if effect == "cpu":
            parts.append(f"cpu: {float(status.get('cpu', 0.0)) * 100:.0f}%")
            if status.get("temp_available"):
                parts.append(f"{float(status.get('temp', 0.0)):.0f}\u00b0C")
        elif effect == "audio":
            mode = status.get("audio_mode", "none")
            if mode and mode != "none":
                parts.append(f"audio: {float(status.get('audio', 0.0)) * 100:.0f}%")
            else:
                parts.append("audio: unavailable")
        if status.get("error"):
            parts.append(f"error: {status['error']}")
        if isinstance(self.controller, RemoteController) and not self.controller.connected:
            parts = ["daemon connection lost - restart it or reopen this window"]
        self.statusBar().showMessage("   |   ".join(parts))

    def _update_live_readout(self, status: dict[str, Any]) -> None:
        """Show the live signal the current effect reacts to."""
        effect = status.get("effect")
        if effect == "cpu":
            load = float(status.get("cpu", 0.0)) * 100.0
            if status.get("temp_available"):
                temp = float(status.get("temp", 0.0))
                self.live_label.setText(f"CPU {temp:.0f}\u00b0C  \u00b7  load {load:.0f}%")
                self.live_label.setToolTip(f"temperature via {status.get('temp_provider', '')}")
            else:
                self.live_label.setText(f"CPU load {load:.0f}%  \u00b7  no temp sensor")
                self.live_label.setToolTip("")
        elif effect == "audio":
            level = float(status.get("audio", 0.0)) * 100.0
            mode = status.get("audio_mode", "none")
            if mode and mode != "none":
                self.live_label.setText(f"Audio {level:.0f}%")
                self.live_label.setToolTip(str(mode))
            else:
                self.live_label.setText("Audio capture unavailable")
                self.live_label.setToolTip("")
        else:
            self.live_label.setText("")

    def _apply_preview(self, color: RGB) -> None:
        self._last_color = color
        self.preview.set_color(color)
        self.preview_label.setText(color.to_hex())
        if self.tray is not None:
            self.tray.setIcon(make_color_icon(color))
            self.tray.setToolTip(f"py_rgb - {self._mode_word()} - {color.to_hex()}")

    def _mode_word(self) -> str:
        return "daemon" if isinstance(self.controller, RemoteController) else "local"

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802, ANN001
        close_to_tray = self.cfg.get("gui", {}).get("close_to_tray", True)
        if getattr(self, "_force_quit", False) or self.tray is None or not close_to_tray:
            self._timer.stop()
            self.controller.close()
            super().closeEvent(event)
            return
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "py_rgb",
            "Still running in the tray. Use Quit py_rgb to exit.",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def run_gui(cfg: dict[str, Any], backend_name: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("py_rgb")
    _apply_dark_palette(app)

    gui_cfg = cfg.get("gui", {})
    tray_wanted = bool(gui_cfg.get("tray", True))
    start_minimized = bool(gui_cfg.get("start_minimized", False))
    # Never quit on last window close while we still expect a tray icon,
    # otherwise a minimised start would exit immediately.
    app.setQuitOnLastWindowClosed(not tray_wanted)

    try:
        window = MainWindow(cfg, backend_name)
    except BackendError as exc:
        log.exception("could not start the GUI")
        QMessageBox.critical(None, "py_rgb", str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 - windowed build has no console
        log.exception("unexpected GUI failure")
        QMessageBox.critical(None, "py_rgb", f"py_rgb failed to start:\n{exc}")
        return 2

    if tray_wanted:
        window.start_tray_retry()

    if start_minimized and tray_wanted:
        window.hide()
    else:
        window.show()
    return app.exec()


def _apply_dark_palette(app: QApplication) -> None:
    palette = QPalette()
    bg, base, text = QColor(32, 33, 36), QColor(24, 25, 28), QColor(225, 226, 230)
    palette.setColor(QPalette.ColorRole.Window, bg)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, bg)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, QColor(45, 46, 50))
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.Highlight, QColor(0, 150, 220))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)
