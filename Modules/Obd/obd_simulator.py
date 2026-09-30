# Modules/Options/obd_simulator.py
from __future__ import annotations

import math
from typing import Dict, Tuple

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QMessageBox, QScrollArea, QPushButton, QSizePolicy, QSlider
)
from PyQt6.QtCore import Qt, QEvent, QTimer

from Modules.Obd._modern_qss import (
    M, apply_modern, modern_button, set_role, set_surface,
)

# Metric config tables: (min, max, step, fmt)
_RANGES: Dict[str, Tuple[float, float]] = {
    "RPM": (0, 9000), "Speed": (0, 250), "Gear": (0, 6),
}
_UNIT_RANGES: Dict[str, Tuple[float, float]] = {
    "%": (0, 100), "°C": (-20, 150), "kPa": (0, 300), "V": (0, 16),
}
_STEPS: Dict[str, float] = {
    "RPM": 100, "Speed": 5, "Gear": 1, "V": 0.5, "°C": 5, "%": 5,
}
_DEFAULTS: Dict[str, float] = {"RPM": 800, "Speed": 0, "Gear": 0}


class OBDSimulatorWindow(QWidget):
    """SIM override GUI. Auto-enables SIM when any override is ON, auto-disables when none are."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Telemetry Debug (SIM)")
        self.setMinimumSize(0, 0)

        from Modules.Obd.telemetry import TelemetryHub, METRIC_REGISTRY
        from Modules.Obd.obd_connection import OBDManager

        self._hub = TelemetryHub.instance()
        self._mgr = OBDManager()
        self._registry = METRIC_REGISTRY
        self._rows: Dict[str, dict] = {}
        self._metric_order: list[str] = []
        self._metric_tiles: Dict[str, QWidget] = {}

        apply_modern(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(M["pad"], M["pad"], M["pad"], M["pad"])
        root.setSpacing(M["gap"])

        # Header
        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel("SIM Telemetry Overrides")
        set_role(title, "title")
        header.addWidget(title, 1)

        self.btn_enable_all = QPushButton()
        self.btn_disable_all = QPushButton()
        self.btn_reset = QPushButton()
        self.btn_accel = QPushButton()
        for btn, txt, role in [(self.btn_enable_all, "Enable all", "primary"),
                               (self.btn_disable_all, "Disable all", None),
                               (self.btn_reset, "Reset values", None),
                               (self.btn_accel, "Hold: Accel", None)]:
            modern_button(btn, text=txt, role=role)
            header.addWidget(btn)

        # Accel hold state
        self._accel_active = False
        self._accel_rpm = 800.0
        self._accel_speed = 0.0
        self._accel_timer = QTimer(self)
        self._accel_timer.setInterval(50)
        self._accel_timer.timeout.connect(self._accel_tick)

        root.addLayout(header)

        # Scenario buttons row
        scenario_row = QHBoxLayout()
        scenario_row.setSpacing(10)
        self.btn_start_10s = QPushButton()
        self.btn_slow_stop = QPushButton()
        self.btn_rev = QPushButton()
        for btn, txt in [(self.btn_start_10s, "10s Start"), (self.btn_slow_stop, "Slow Stop"),
                         (self.btn_rev, "Rev")]:
            modern_button(btn, text=txt, min_width=120)
            scenario_row.addWidget(btn)
        scenario_row.addStretch(1)
        root.addLayout(scenario_row)

        # Scenario timer
        self._scenario_timer = QTimer(self)
        self._scenario_timer.setInterval(50)  # 20 Hz
        self._scenario_timer.timeout.connect(self._scenario_tick)
        self._scenario_elapsed = 0.0
        self._scenario_type = ""  # "start", "stop", "rev"

        # Status line
        self.lbl_status = QLabel("")
        set_role(self.lbl_status, "dim")
        root.addWidget(self.lbl_status)

        # Scrollable metric grid
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self.scroll, 1)
        # The window's own resizeEvent fires before the viewport has been laid
        # out, so the column count it computes is a frame stale. Watch the
        # viewport instead — event-driven, and the reflow is a no-op unless the
        # column count actually changed.
        self._grid_cols = -1
        self.scroll.viewport().installEventFilter(self)

        content = QWidget()
        self.scroll.setWidget(content)
        self.grid = QGridLayout(content)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(M["gap"])
        self.grid.setVerticalSpacing(M["gap"])

        metrics = sorted(self._registry.keys())
        if "Gear" not in metrics:
            metrics.append("Gear")
        for name in metrics:
            self._metric_order.append(name)
            self._metric_tiles[name] = self._make_metric_tile(name)
        self._reflow_metric_tiles()

        # Wiring
        self.btn_enable_all.clicked.connect(self._enable_all)
        self.btn_disable_all.clicked.connect(self._disable_all)
        self.btn_reset.clicked.connect(self._reset_values)
        self.btn_accel.pressed.connect(self._on_accel_pressed)
        self.btn_accel.released.connect(self._on_accel_released)
        self.btn_start_10s.clicked.connect(lambda: self._start_scenario("start"))
        self.btn_slow_stop.clicked.connect(lambda: self._start_scenario("stop"))
        self.btn_rev.clicked.connect(lambda: self._start_scenario("rev"))

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(700)
        self._refresh_status()
        self._sync_sim_enabled()

    # ---- Layout ----
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow_metric_tiles()

    def eventFilter(self, obj, event):
        if (
            hasattr(self, "scroll")
            and obj is self.scroll.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            self._reflow_metric_tiles()
        return super().eventFilter(obj, event)

    def _reflow_metric_tiles(self, force: bool = False):
        if not hasattr(self, "grid"):
            return

        vp = self.scroll.viewport() if hasattr(self, "scroll") else None
        w = vp.width() if vp else self.width()
        # ~300 px is the narrowest a metric card reads well at; cap at the grid width.
        cols = max(1, min(6, w // 300))
        if not force and cols == getattr(self, "_grid_cols", -1):
            return
        self._grid_cols = cols

        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

        for c in range(6):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)
        for i, name in enumerate(self._metric_order):
            tile = self._metric_tiles.get(name)
            if tile:
                self.grid.addWidget(tile, i // cols, i % cols)

    # ---- Metric config helpers ----
    def _unit_for(self, name: str) -> str:
        return str(self._registry.get(name, {}).get("unit", "") or "") if name in self._registry else ""

    def _range_for(self, name: str, unit: str) -> Tuple[float, float]:
        return _RANGES.get(name) or _UNIT_RANGES.get(unit, (0, 300))

    def _step_for(self, name: str, unit: str) -> float:
        return _STEPS.get(name) or _STEPS.get(unit, 5)

    def _fmt_value(self, v: float, unit: str, name: str) -> str:
        if name in ("RPM", "Speed", "Gear") or unit in ("°C", "%", "kPa"):
            return f"{int(round(v))}"
        return f"{v:.1f}"

    # ---- Tile builder ----
    def _make_metric_tile(self, name: str) -> QWidget:
        unit = self._unit_for(name)
        vmin, vmax = self._range_for(name, unit)
        step = self._step_for(name, unit)

        if name in ("RPM", "Speed"):
            v0 = float(self._hub.get_sim_value(name, _DEFAULTS.get(name, 0)))
        else:
            v0 = float(self._hub.get_sim_override_value(name, 0.0))
        v0 = max(vmin, min(vmax, v0))

        slider_max = max(1, int(round((vmax - vmin) / step)))
        slider_init = max(0, min(slider_max, int(round((v0 - vmin) / step))))

        wrap = QFrame()
        set_surface(wrap, "card")
        wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        wrap.setMinimumHeight(142)

        v_lay = QVBoxLayout(wrap)
        v_lay.setContentsMargins(18, 16, 18, 16)
        v_lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        lbl_title = QLabel(name)
        set_role(lbl_title, "section")
        head.addWidget(lbl_title)
        if unit:
            lbl_unit = QLabel(unit)
            set_role(lbl_unit, "faint")
            head.addWidget(lbl_unit)
        head.addStretch(1)

        val_lbl = QLabel(self._fmt_value(v0, unit, name))
        set_role(val_lbl, "value")
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(val_lbl)
        v_lay.addLayout(head)

        sld = QSlider(Qt.Orientation.Horizontal)
        sld.setRange(0, slider_max)
        sld.setValue(slider_init)
        v_lay.addWidget(sld)

        tgl = QPushButton()
        tgl.setCheckable(True)
        modern_button(tgl, text="Override")
        tgl.setChecked(bool(self._hub.get_sim_override_enabled(name)))
        v_lay.addWidget(tgl)

        self._rows[name] = dict(unit=unit, min=vmin, max=vmax, step=step,
                                value=v0, lbl=val_lbl, tgl=tgl, sld=sld, sld_max=slider_max)

        tgl.toggled.connect(lambda on, n=name: self._on_toggle_override(n, on))

        def _on_slider(sv, n=name, vmin_=vmin, step_=step, unit_=unit):
            actual = vmin_ + sv * step_
            if n == "Gear":
                actual = float(int(round(actual)))
            r = self._rows[n]
            r["value"] = actual
            r["lbl"].setText(self._fmt_value(actual, unit_, n))
            try:
                if self._hub.get_sim_override_enabled(n):
                    self._apply_value_to_hub(n, actual)
            except Exception:
                pass

        sld.valueChanged.connect(_on_slider)
        return wrap

    # ---- Status / SIM sync ----
    def _refresh_status(self):
        connected = bool(self._mgr.is_connected()) if self._mgr else False
        any_on = self._any_override_on()
        sim_on = bool(self._hub.is_sim_enabled()) if hasattr(self._hub, "is_sim_enabled") else any_on
        if connected:
            self.lbl_status.setText("Status: OBD connected. Overrides locked.")
        else:
            self.lbl_status.setText(f"Status: OBD disconnected. Overrides {'ACTIVE' if any_on else 'idle'} (SIM {'ON' if sim_on else 'OFF'})")

    def _any_override_on(self) -> bool:
        return any(bool(self._hub.get_sim_override_enabled(n)) for n in self._rows)

    def _sync_sim_enabled(self):
        try:
            self._hub.set_sim_enabled(self._any_override_on())
        except Exception:
            pass

    # ---- Hub helpers ----
    def _apply_value_to_hub(self, name: str, value: float):
        try:
            if name in ("RPM", "Speed"):
                self._hub.set_sim_value(name, float(value))
            self._hub.set_sim_override_value(name, float(value))
        except Exception:
            pass

    def _update_slider_for(self, name: str, value: float):
        r = self._rows.get(name)
        if not r:
            return
        sv = max(0, min(int(r["sld_max"]), int(round((value - r["min"]) / r["step"]))))
        r["sld"].blockSignals(True)
        r["sld"].setValue(sv)
        r["sld"].blockSignals(False)
        r["value"] = value
        r["lbl"].setText(self._fmt_value(value, r["unit"], name))

    # ---- UI actions ----
    def _on_toggle_override(self, name: str, enabled: bool):
        if enabled:
            try:
                if self._mgr.is_connected():
                    QMessageBox.warning(self, "SIM locked", "OBD is connected. Disconnect the adapter to use overrides.")
                    btn = self._rows[name]["tgl"]
                    btn.blockSignals(True)
                    btn.setChecked(False)
                    btn.blockSignals(False)
                    return
            except Exception:
                pass
        try:
            self._hub.set_sim_override_enabled(name, bool(enabled))
        except Exception:
            pass
        if enabled:
            try:
                self._apply_value_to_hub(name, float(self._rows[name]["value"]))
            except Exception:
                pass
        self._sync_sim_enabled()

    def _set_all_overrides(self, enabled: bool):
        if enabled:
            try:
                if self._mgr.is_connected():
                    QMessageBox.warning(self, "SIM locked", "OBD is connected. Disconnect the adapter to use overrides.")
                    return
            except Exception:
                pass
        for name, r in self._rows.items():
            r["tgl"].blockSignals(True)
            r["tgl"].setChecked(enabled)
            r["tgl"].blockSignals(False)
            try:
                self._hub.set_sim_override_enabled(name, enabled)
            except Exception:
                pass
            if enabled:
                self._apply_value_to_hub(name, float(r["value"]))
        self._sync_sim_enabled()

    def _enable_all(self):
        self._set_all_overrides(True)

    def _disable_all(self):
        self._set_all_overrides(False)

    def _reset_values(self):
        for name, r in self._rows.items():
            v = max(r["min"], min(r["max"], _DEFAULTS.get(name, 0.0)))
            if name == "Gear":
                v = float(int(round(v)))
            sv = max(0, min(int(r["sld_max"]), int(round((v - r["min"]) / r["step"]))))
            r["sld"].blockSignals(True)
            r["sld"].setValue(sv)
            r["sld"].blockSignals(False)
            r["value"] = v
            r["lbl"].setText(self._fmt_value(v, r["unit"], name))
            try:
                if self._hub.get_sim_override_enabled(name):
                    self._apply_value_to_hub(name, v)
            except Exception:
                pass

    # ---- Accelerate hold ----
    def _on_accel_pressed(self):
        try:
            self._accel_rpm = float(self._hub.get_sim_value("RPM", self._accel_rpm))
            self._accel_speed = float(self._hub.get_sim_value("Speed", self._accel_speed))
        except Exception:
            pass
        self._accel_active = True
        self._ensure_accel_overrides()
        self._accel_timer.start()

    def _on_accel_released(self):
        self._accel_active = False
        # Immediately reset speed to 0 on release
        self._accel_speed = 0.0
        self._apply_value_to_hub("Speed", 0.0)
        self._update_slider_for("Speed", 0.0)

    def _accel_tick(self):
        if self._accel_active:
            self._accel_rpm = min(7000, self._accel_rpm + max(15, int(70 * math.exp(-self._accel_rpm / 6000))))
            self._accel_speed = min(250, self._accel_speed + 0.6)
            throttle = 100.0
        else:
            self._accel_rpm = max(800, self._accel_rpm - 35)
            throttle = 0.0
            if self._accel_rpm <= 800:
                self._accel_timer.stop()

        for metric, val in [("RPM", self._accel_rpm), ("Speed", self._accel_speed)]:
            self._apply_value_to_hub(metric, val)
            self._update_slider_for(metric, val)
        if "Throttle Pos" in self._rows:
            self._apply_value_to_hub("Throttle Pos", throttle)
            self._update_slider_for("Throttle Pos", throttle)

    # ---- Scenario simulations ----
    def _start_scenario(self, scenario_type: str):
        self._scenario_timer.stop()
        self._scenario_type = scenario_type
        self._scenario_elapsed = 0.0
        self._ensure_accel_overrides()
        if "Gear" in self._rows:
            if not self._hub.get_sim_override_enabled("Gear"):
                self._rows["Gear"]["tgl"].blockSignals(True)
                self._rows["Gear"]["tgl"].setChecked(True)
                self._rows["Gear"]["tgl"].blockSignals(False)
                self._hub.set_sim_override_enabled("Gear", True)
            self._sync_sim_enabled()
        self._scenario_timer.start()

    def _scenario_tick(self):
        self._scenario_elapsed += 0.05  # 50ms per tick
        t = self._scenario_elapsed

        if self._scenario_type == "start":
            rpm, speed, throttle, gear = self._calc_start_scenario(t)
        elif self._scenario_type == "stop":
            rpm, speed, throttle, gear = self._calc_stop_scenario(t)
        elif self._scenario_type == "rev":
            rpm, speed, throttle, gear = self._calc_rev_scenario(t)
        else:
            self._scenario_timer.stop()
            return

        # End after 20 seconds
        if t >= 20.0:
            self._scenario_timer.stop()

        for metric, val in [("RPM", rpm), ("Speed", speed)]:
            self._apply_value_to_hub(metric, val)
            self._update_slider_for(metric, val)
        if "Throttle Pos" in self._rows:
            self._apply_value_to_hub("Throttle Pos", throttle)
            self._update_slider_for("Throttle Pos", throttle)
        if "Gear" in self._rows:
            self._apply_value_to_hub("Gear", gear)
            self._update_slider_for("Gear", gear)

    def _calc_start_scenario(self, t: float):
        """Realistic launch: 1st→2nd→3rd→4th→5th over 20 seconds.
        Shifts around 3500 RPM, realistic acceleration and throttle."""
        if t < 3.0:
            # 1st gear: launch from idle, building RPM to shift point
            p = t / 3.0
            rpm = 800 + 2700 * p          # 800 → 3500
            speed = 20 * p                 # 0 → 20 km/h
            throttle = 50.0 + 15.0 * p    # 50 → 65%
            gear = 1
        elif t < 3.5:
            # Shift 1→2 (RPM drops, speed coasts up slightly)
            p = (t - 3.0) / 0.5
            rpm = 3500 - 1500 * p          # 3500 → 2000
            speed = 20 + 2 * p             # 20 → 22
            throttle = 15.0
            gear = 2
        elif t < 7.0:
            # 2nd gear pull
            p = (t - 3.5) / 3.5
            rpm = 2000 + 1500 * p          # 2000 → 3500
            speed = 22 + 23 * p            # 22 → 45
            throttle = 45.0 + 10.0 * p     # 45 → 55%
            gear = 2
        elif t < 7.5:
            # Shift 2→3
            p = (t - 7.0) / 0.5
            rpm = 3500 - 1200 * p          # 3500 → 2300
            speed = 45 + 2 * p             # 45 → 47
            throttle = 12.0
            gear = 3
        elif t < 11.5:
            # 3rd gear pull
            p = (t - 7.5) / 4.0
            rpm = 2300 + 1200 * p          # 2300 → 3500
            speed = 47 + 23 * p            # 47 → 70
            throttle = 40.0 + 10.0 * p     # 40 → 50%
            gear = 3
        elif t < 12.0:
            # Shift 3→4
            p = (t - 11.5) / 0.5
            rpm = 3500 - 1100 * p          # 3500 → 2400
            speed = 70 + 2 * p             # 70 → 72
            throttle = 10.0
            gear = 4
        elif t < 16.0:
            # 4th gear pull
            p = (t - 12.0) / 4.0
            rpm = 2400 + 1100 * p          # 2400 → 3500
            speed = 72 + 18 * p            # 72 → 90
            throttle = 35.0 + 10.0 * p     # 35 → 45%
            gear = 4
        elif t < 16.5:
            # Shift 4→5
            p = (t - 16.0) / 0.5
            rpm = 3500 - 1000 * p          # 3500 → 2500
            speed = 90 + 1 * p             # 90 → 91
            throttle = 8.0
            gear = 5
        else:
            # 5th gear cruise
            p = min(1.0, (t - 16.5) / 3.5)
            rpm = 2500 + 400 * p           # 2500 → 2900
            speed = 91 + 9 * p             # 91 → 100
            throttle = 25.0 - 10.0 * p     # 25 → 15% (easing off)
            gear = 5
        return rpm, speed, throttle, gear

    def _calc_stop_scenario(self, t: float):
        """Deceleration from 5th gear cruise to standstill over 20 seconds."""
        if t < 3.0:
            # 5th gear coasting — lift off throttle
            p = t / 3.0
            rpm = 2900 - 500 * p           # 2900 → 2400
            speed = 100 - 15 * p           # 100 → 85
            throttle = 0.0
            gear = 5
        elif t < 3.5:
            # Downshift 5→4
            p = (t - 3.0) / 0.5
            rpm = 2400 + 400 * p           # 2400 → 2800
            speed = 85 - 3 * p             # 85 → 82
            throttle = 0.0
            gear = 4
        elif t < 7.0:
            # 4th gear engine braking
            p = (t - 3.5) / 3.5
            rpm = 2800 - 1000 * p          # 2800 → 1800
            speed = 82 - 22 * p            # 82 → 60
            throttle = 0.0
            gear = 4
        elif t < 7.5:
            # Downshift 4→3
            p = (t - 7.0) / 0.5
            rpm = 1800 + 600 * p           # 1800 → 2400
            speed = 60 - 2 * p             # 60 → 58
            throttle = 0.0
            gear = 3
        elif t < 11.0:
            # 3rd gear engine braking
            p = (t - 7.5) / 3.5
            rpm = 2400 - 1000 * p          # 2400 → 1400
            speed = 58 - 23 * p            # 58 → 35
            throttle = 0.0
            gear = 3
        elif t < 11.5:
            # Downshift 3→2
            p = (t - 11.0) / 0.5
            rpm = 1400 + 700 * p           # 1400 → 2100
            speed = 35 - 2 * p             # 35 → 33
            throttle = 0.0
            gear = 2
        elif t < 14.5:
            # 2nd gear engine braking
            p = (t - 11.5) / 3.0
            rpm = 2100 - 900 * p           # 2100 → 1200
            speed = 33 - 18 * p            # 33 → 15
            throttle = 0.0
            gear = 2
        elif t < 15.0:
            # Downshift 2→1
            p = (t - 14.5) / 0.5
            rpm = 1200 + 500 * p           # 1200 → 1700
            speed = 15 - 2 * p             # 15 → 13
            throttle = 0.0
            gear = 1
        elif t < 18.0:
            # 1st gear to near stop
            p = (t - 15.0) / 3.0
            rpm = 1700 - 900 * p           # 1700 → 800
            speed = 13 - 13 * p            # 13 → 0
            throttle = 0.0
            gear = 1
        else:
            # Idle
            rpm = 800
            speed = 0
            throttle = 0.0
            gear = 0
        return rpm, max(0, speed), throttle, gear

    def _calc_rev_scenario(self, t: float):
        """Rev blips at standstill for 20 seconds."""
        blips = [
            (0.0,  1.5, 3500),   # gentle warm-up blip
            (2.5,  1.8, 4500),   # medium blip
            (5.5,  1.2, 3000),   # short low blip
            (7.5,  2.0, 5000),   # big blip
            (10.5, 1.5, 3500),   # medium blip
            (13.0, 2.2, 5500),   # big aggressive blip
            (16.0, 1.5, 4000),   # medium blip
            (18.5, 1.5, 3000),   # cool-down blip
        ]
        rpm = 800.0
        for start, dur, peak in blips:
            if start <= t < start + dur:
                half = dur / 2.0
                if t < start + half:
                    p = (t - start) / half
                    rpm = 800 + (peak - 800) * p
                else:
                    p = (t - start - half) / half
                    rpm = peak - (peak - 800) * p
                break

        throttle = max(0.0, (rpm - 800) / 50.0)
        return rpm, 0.0, min(100.0, throttle), 0

    def _ensure_accel_overrides(self):
        for metric in ("RPM", "Throttle Pos", "Speed"):
            if metric not in self._rows:
                continue
            try:
                if not self._hub.get_sim_override_enabled(metric):
                    self._rows[metric]["tgl"].blockSignals(True)
                    self._rows[metric]["tgl"].setChecked(True)
                    self._rows[metric]["tgl"].blockSignals(False)
                    self._hub.set_sim_override_enabled(metric, True)
            except Exception:
                pass
        self._sync_sim_enabled()

    def closeEvent(self, event):
        try:
            self._hub.set_sim_enabled(False)
        except Exception:
            pass
        return super().closeEvent(event)
