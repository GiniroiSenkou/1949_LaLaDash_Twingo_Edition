from __future__ import annotations

import threading
import time

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from preferences import DATA_SETTINGS
from Modules.Runtime import settings_cache


class OBDBridge(QObject):
    """Exposes OBD/CAN connection management to QML."""

    connectionChanged = pyqtSignal()
    portsChanged = pyqtSignal()
    canStatsChanged = pyqtSignal()
    canProbeResultChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._connected = False
        self._connecting = False
        self._port = settings_cache.get("OBD", "port", fallback="/dev/ttyUSB0", path=DATA_SETTINGS) or "/dev/ttyUSB0"
        self._baud = settings_cache.getint("OBD", "baud", fallback=115200, path=DATA_SETTINGS)
        self._protocol = settings_cache.get("OBD", "protocol_mode", fallback="obd2", path=DATA_SETTINGS) or "obd2"
        self._can_interface = settings_cache.get("OBD", "can_interface", fallback="slcan", path=DATA_SETTINGS) or "slcan"
        self._can_channel = settings_cache.get("OBD", "can_channel", fallback="", path=DATA_SETTINGS) or ""
        self._can_bitrate = settings_cache.getint("OBD", "can_bitrate", fallback=500000, path=DATA_SETTINGS)
        self._connection_error = ""
        self._available_ports: list[str] = []

        # Proof-of-life cache (refreshed by the page-gated 1 Hz timer).
        self._can_is_receiving = False
        self._can_fps = 0.0
        self._can_unique_ids = 0
        self._can_last_frame_age_ms = -1
        self._can_last_error = ""
        self._can_probe_result = ""

        # Page-visibility-gated 1 Hz refresh. Started by the Obd settings
        # page's onVisibleChanged; stopped when the user leaves. Pi 4B
        # constraint: zero work outside the settings page.
        self._can_stats_timer = QTimer(self)
        self._can_stats_timer.setInterval(1000)
        self._can_stats_timer.timeout.connect(self._refresh_can_stats)
        # The link comes and goes behind this bridge's back — the boot attempt,
        # the coordinator's idle retry, a protocol switch. Follow the
        # coordinator's own connected flag (its `changed` is queued onto the
        # GUI thread) so Settings → OBD never shows "Disconnected" over a
        # live link again.
        try:
            from Modules.Runtime.telemetry_coordinator import TelemetryCoordinator
            TelemetryCoordinator.instance().changed.connect(self._on_coordinator_tick)
        except Exception:
            pass

        self._refresh_state()

    def _manager(self):
        """Return the active connection manager based on current protocol setting.

        CAN-BUS mode on the car's STN1170 is *not* a different transport: the
        PID link stays python-obd on OBDManager and the raw monitor rides the
        same port (Modules/Obd/stn_session.py). python-can (slcan/socketcan)
        remains available for non-ELM hardware via [OBD] can_backend."""
        if self._protocol == "can":
            backend = (settings_cache.get("OBD", "can_backend", fallback="stn", path=DATA_SETTINGS) or "stn").strip().lower()
            if backend == "python-can":
                from Modules.Obd.can_connection import CANConnectionManager
                return CANConnectionManager()
        from Modules.Obd.obd_connection import OBDManager
        return OBDManager.instance()

    def _on_coordinator_tick(self, _snap):
        try:
            from Modules.Runtime.telemetry_coordinator import TelemetryCoordinator
            now_connected = bool(TelemetryCoordinator.instance().connected())
        except Exception:
            return
        if now_connected != self._connected:
            self._refresh_state()
            self.connectionChanged.emit()

    def _refresh_state(self):
        try:
            mgr = self._manager()
            self._connected = bool(mgr.is_connected())
            port = mgr.get_port()
            baud = mgr.get_baudrate()
            if port:
                self._port = port
            if baud:
                self._baud = int(baud)
        except Exception:
            self._connected = False

    # ── connection state ──────────────────────────────────────────────────
    @pyqtProperty(bool, notify=connectionChanged)
    def connected(self):
        return self._connected

    @pyqtProperty(bool, notify=connectionChanged)
    def connecting(self):
        return self._connecting

    @pyqtProperty(str, notify=connectionChanged)
    def port(self):
        return self._port

    @pyqtProperty(int, notify=connectionChanged)
    def baud(self):
        return self._baud

    @pyqtProperty(str, notify=connectionChanged)
    def protocol(self):
        return self._protocol

    @pyqtProperty(str, notify=connectionChanged)
    def canInterface(self):
        return self._can_interface

    @pyqtProperty(str, notify=connectionChanged)
    def canChannel(self):
        return self._can_channel

    @pyqtProperty(int, notify=connectionChanged)
    def canBitrate(self):
        return self._can_bitrate

    @pyqtProperty(str, notify=connectionChanged)
    def connectionErrorText(self):
        return self._connection_error

    @pyqtProperty(list, notify=portsChanged)
    def availablePorts(self):
        return self._available_ports

    # ── CAN proof-of-life (live RX stats) ─────────────────────────────────
    @pyqtProperty(bool, notify=canStatsChanged)
    def canIsReceiving(self):
        return self._can_is_receiving

    @pyqtProperty(float, notify=canStatsChanged)
    def canFramesPerSec(self):
        return self._can_fps

    @pyqtProperty(int, notify=canStatsChanged)
    def canUniqueIds(self):
        return self._can_unique_ids

    @pyqtProperty(int, notify=canStatsChanged)
    def canLastFrameAgeMs(self):
        return self._can_last_frame_age_ms

    @pyqtProperty(str, notify=canStatsChanged)
    def canLastError(self):
        return self._can_last_error

    @pyqtProperty(str, notify=canProbeResultChanged)
    def canProbeResult(self):
        return self._can_probe_result

    @pyqtSlot()
    def activateCanStatsPolling(self):
        """Called from QML when the Obd/Diagnose settings page becomes
        visible. Only spins up the 1 Hz refresh while the user is looking
        at it (Pi 4B budget)."""
        if not self._can_stats_timer.isActive():
            self._can_stats_timer.start()
        # Immediate first read so the UI doesn't have a 1 s "blank" delay.
        self._refresh_can_stats()

    @pyqtSlot()
    def deactivateCanStatsPolling(self):
        """Called from QML on page hide / navigate-away."""
        if self._can_stats_timer.isActive():
            self._can_stats_timer.stop()

    @pyqtSlot()
    def probeCanTraffic(self):
        """3-second snapshot: delta the manager's frames_total over a fixed
        window and report a one-line verdict to QML. Runs on a daemon
        thread so the UI never blocks. Read-only — no extra subscribers."""
        # Only meaningful in CAN mode while connected.
        if self._protocol != "can":
            self._can_probe_result = "Switch to CAN-bus mode first."
            self.canProbeResultChanged.emit()
            return
        mgr = self._manager()
        if not getattr(mgr, "is_connected", lambda: False)():
            self._can_probe_result = "Not connected. Press Connect first."
            self.canProbeResultChanged.emit()
            return
        if not hasattr(mgr, "frames_total"):
            self._can_probe_result = "CAN manager has no live stats — restart the app."
            self.canProbeResultChanged.emit()
            return

        self._can_probe_result = "Listening…"
        self.canProbeResultChanged.emit()

        def _run():
            try:
                t0 = int(mgr.frames_total())
                ids0 = int(getattr(mgr, "unique_id_count", lambda: 0)())
                time.sleep(3.0)
                t1 = int(mgr.frames_total())
                ids1 = int(getattr(mgr, "unique_id_count", lambda: 0)())
            except Exception as exc:
                self._can_probe_result = f"Probe failed: {type(exc).__name__}: {exc}"
                self.canProbeResultChanged.emit()
                return
            delta = max(0, t1 - t0)
            ids_seen = max(ids1, ids0)
            if delta > 0:
                self._can_probe_result = (
                    f"OK — {delta} frames in 3 s ({delta // 3} fps), {ids_seen} unique IDs."
                )
            else:
                last_err = ""
                try:
                    last_err = mgr.last_error() if hasattr(mgr, "last_error") else ""
                except Exception:
                    last_err = ""
                hint = (f" {last_err}" if last_err else
                        " Check bitrate (500k → 250k → 125k) and wiring.")
                self._can_probe_result = "NO TRAFFIC — bus open but silent." + hint
            self.canProbeResultChanged.emit()

        threading.Thread(target=_run, name="CANProbe", daemon=True).start()

    def _refresh_can_stats(self):
        """1 Hz pull from the manager's lock-protected stats. Page-gated."""
        if self._protocol != "can":
            # Nothing to show — clear cached values so QML reflects the off
            # state when the user toggles between modes on the page.
            changed = False
            if self._can_is_receiving:
                self._can_is_receiving = False; changed = True
            if self._can_fps != 0.0:
                self._can_fps = 0.0; changed = True
            if self._can_unique_ids != 0:
                self._can_unique_ids = 0; changed = True
            if self._can_last_frame_age_ms != -1:
                self._can_last_frame_age_ms = -1; changed = True
            if self._can_last_error:
                self._can_last_error = ""; changed = True
            if changed:
                self.canStatsChanged.emit()
            return

        mgr = self._manager()
        try:
            snap = mgr.stats_snapshot() if hasattr(mgr, "stats_snapshot") else None
        except Exception:
            snap = None
        if not snap:
            return
        new_fps = float(snap.get("fps") or 0.0)
        new_unique = int(snap.get("unique_ids") or 0)
        new_age = int(snap.get("age_ms") if snap.get("age_ms") is not None else -1)
        new_recv = new_age >= 0 and new_age <= 3000
        try:
            new_err = mgr.last_error() if hasattr(mgr, "last_error") else ""
        except Exception:
            new_err = ""

        if (
            new_fps != self._can_fps
            or new_unique != self._can_unique_ids
            or new_age != self._can_last_frame_age_ms
            or new_recv != self._can_is_receiving
            or (new_err or "") != self._can_last_error
        ):
            self._can_fps = new_fps
            self._can_unique_ids = new_unique
            self._can_last_frame_age_ms = new_age
            self._can_is_receiving = new_recv
            self._can_last_error = new_err or ""
            self.canStatsChanged.emit()

    # ── connection control ────────────────────────────────────────────────
    @pyqtSlot()
    def connect(self):
        if self._connected or self._connecting:
            return
        self._connecting = True
        self._connection_error = ""
        self.connectionChanged.emit()

        # quick_connect() blocks on the ELM327 handshake (protocol detect +
        # PID probe) for several seconds — run it off the Qt main thread so
        # the UI stays responsive. The cross-thread connectionChanged.emit()
        # is delivered as a queued connection back on the main thread.
        def _worker():
            error = ""
            try:
                ok = self._manager().quick_connect()
                self._refresh_state()
                if not ok:
                    mgr = self._manager()
                    if hasattr(mgr, "last_error"):
                        error = mgr.last_error()
                    elif not self._connected:
                        error = "Connection failed"
            except Exception as e:
                self._connected = False
                error = str(e)
            self._connection_error = error
            self._connecting = False
            self.connectionChanged.emit()

        threading.Thread(target=_worker, name="OBDConnect", daemon=True).start()

    @pyqtSlot()
    def disconnect(self):
        try:
            self._manager().disconnect()
        except Exception:
            pass
        self._refresh_state()
        self.connectionChanged.emit()

    @pyqtSlot()
    def scanPorts(self):
        ports = []
        try:
            import serial.tools.list_ports
            for p in serial.tools.list_ports.comports():
                ports.append(p.device)
        except Exception:
            pass
        self._available_ports = sorted(ports)
        self.portsChanged.emit()

    @pyqtSlot(str)
    def setPort(self, port):
        self._port = port
        settings_cache.set_value("OBD", "port", port, path=DATA_SETTINGS)
        self.connectionChanged.emit()


    @pyqtSlot(str)
    def setProtocol(self, protocol):
        self._protocol = protocol
        settings_cache.set_value("OBD", "protocol_mode", protocol, path=DATA_SETTINGS)
        self.connectionChanged.emit()
        # Switching to/from CAN-BUS while the PID link is up: attach or drop
        # the STN session in place rather than forcing a reconnect. The
        # handshake (STI/STBR) talks to the adapter, so off the Qt thread.
        try:
            from Modules.Obd.obd_connection import OBDManager
            mgr = OBDManager.instance()
            if not mgr.is_connected():
                return
            if protocol == "can" and mgr.get_session() is None and mgr.can_monitor_enabled():
                conn = mgr.get_connection()
                threading.Thread(target=lambda: mgr._attach_session(conn), name="StnAttach", daemon=True).start()
            elif protocol != "can" and mgr.get_session() is not None:
                threading.Thread(target=mgr.drop_session, name="StnDrop", daemon=True).start()
        except Exception:
            pass

    @pyqtSlot(str)
    def setCanInterface(self, iface):
        self._can_interface = iface
        settings_cache.set_value("OBD", "can_interface", iface, path=DATA_SETTINGS)
        self.connectionChanged.emit()

    @pyqtSlot(str)
    def setCanChannel(self, channel):
        self._can_channel = channel
        settings_cache.set_value("OBD", "can_channel", channel, path=DATA_SETTINGS)
        self.connectionChanged.emit()

    @pyqtSlot(int)
    def setCanBitrate(self, bitrate):
        self._can_bitrate = bitrate
        settings_cache.set_value("OBD", "can_bitrate", str(bitrate), path=DATA_SETTINGS)
        self.connectionChanged.emit()

    @pyqtSlot()
    def deepScan(self):
        """Attempt a deep scan (full protocol search)."""
        if self._connected or self._connecting:
            return
        self._connecting = True
        self.connectionChanged.emit()

        # scan_connect() sweeps every port×baud combo — even slower than
        # quick_connect(). Run it off the Qt main thread.
        def _worker():
            try:
                ok, _trace = self._manager().scan_connect()
                self._connected = bool(ok)
                self._refresh_state()
            except Exception:
                self._connected = False
            self._connecting = False
            self.connectionChanged.emit()

        threading.Thread(target=_worker, name="OBDDeepScan", daemon=True).start()

    @pyqtSlot()
    def testConnection(self):
        """Test current OBD connection."""
        try:
            mgr = self._manager()
            if hasattr(mgr, "test_connection"):
                self._connected = bool(mgr.test_connection())
            else:
                self._connected = bool(mgr.is_connected())
            self._refresh_state()
        except Exception:
            self._connected = False
        self.connectionChanged.emit()

    def _require_obd2(self, feature: str) -> str | None:
        if self._protocol == "can":
            return f"{feature}: CAN-BUS mode is passive here; OBD-II PID queries are disabled."
        return None

    @staticmethod
    def _format_response(resp) -> str:
        if not resp or resp.is_null():
            return "N/A"
        value = getattr(resp, "value", None)
        magnitude = getattr(value, "magnitude", None)
        units = getattr(value, "units", None)
        if magnitude is not None:
            return f"{magnitude} {units}".strip() if units else f"{magnitude}"
        return str(value)

    def _query_obd(self, command) -> tuple[bool, str]:
        if command is None:
            return False, "N/A"
        try:
            from Modules.Obd.obd_connection import safe_query
        except Exception as e:
            return False, f"query layer unavailable: {e}"
        try:
            resp = safe_query(command)
            if not resp or resp.is_null():
                return False, "N/A"
            return True, self._format_response(resp)
        except Exception as e:
            return False, f"error: {e}"

    def _read_dtcs(self, command, label: str) -> str:
        try:
            from Modules.Obd.obd_connection import safe_query
        except Exception as e:
            return f"{label}: query layer unavailable: {e}"
        if command is None:
            return f"{label}: N/A"
        try:
            resp = safe_query(command)
            if not resp or resp.is_null():
                return f"{label}: N/A"
            dtcs = resp.value or []
            if not dtcs:
                return f"{label}: none."
            return f"{label}: " + ", ".join(f"{code} ({desc})" for code, desc in dtcs)
        except Exception as e:
            return f"{label}: error: {e}"

    @pyqtSlot(str, result=str)
    def runDiagnostic(self, name):
        label = str(name or "").strip()
        if not label:
            return "Diagnostic: no command selected."

        if label == "Deep Scan":
            try:
                ok, trace = self._manager().scan_connect()
                self._connected = bool(ok)
                self._refresh_state()
                self.connectionChanged.emit()
                lines = [str(line) for line in (trace or [])]
                lines.append("Deep scan: connected." if ok else "Deep scan: no stable connection.")
                return "\n".join(lines)
            except Exception as e:
                self._connected = False
                self.connectionChanged.emit()
                return f"Deep scan error: {e}"

        if label in ("Adapter Info", "Read VIN", "Stored DTCs", "Pending DTCs",
                     "Permanent DTCs", "Freeze-frame", "Supported PIDs",
                     "One-shot Snapshot", "Sensor Health Pack", "Performance Snapshot"):
            blocked = self._require_obd2(label)
            if blocked:
                return blocked

        try:
            import obd
        except Exception as e:
            return f"{label}: python-OBD unavailable: {e}"

        if label == "Adapter Info":
            _, voltage = self._query_obd(obd.commands.ELM_VOLTAGE)
            _, version = self._query_obd(obd.commands.ELM_VERSION)
            return f"Adapter voltage: {voltage} | Adapter version: {version}"
        if label == "Read VIN":
            ok, vin = self._query_obd(obd.commands.VIN)
            return f"VIN: {vin if ok else 'N/A'}"
        if label == "Stored DTCs":
            _, mil = self._query_obd(obd.commands.STATUS)
            return f"MIL/Monitor status: {mil}\n" + self._read_dtcs(obd.commands.GET_DTC, "Stored DTCs")
        if label == "Pending DTCs":
            return self._read_dtcs(getattr(obd.commands, "PENDING_DTC", None), "Pending DTCs")
        if label == "Permanent DTCs":
            return self._read_dtcs(getattr(obd.commands, "PERMANENT_DTC", None), "Permanent DTCs")
        if label == "Freeze-frame":
            ok, value = self._query_obd(getattr(obd.commands, "FREEZE_DTC", None))
            return f"Freeze-frame DTC: {value if ok else 'N/A'}"
        if label == "Supported PIDs":
            lines = []
            for key in ("PIDS_A", "PIDS_B", "PIDS_C"):
                _, value = self._query_obd(getattr(obd.commands, key, None))
                lines.append(f"{key}={value}")
            return "Supported PIDs: " + " | ".join(lines)
        if label == "One-shot Snapshot":
            items = [
                ("RPM", obd.commands.RPM),
                ("Speed", obd.commands.SPEED),
                ("Coolant", obd.commands.COOLANT_TEMP),
                ("Intake Temp", obd.commands.INTAKE_TEMP),
                ("Oil Temp", getattr(obd.commands, "OIL_TEMP", None)),
                ("Throttle", obd.commands.THROTTLE_POS),
                ("Battery", obd.commands.ELM_VOLTAGE),
            ]
            return self._snapshot_line("One-shot snapshot", items)
        if label == "Sensor Health Pack":
            items = [
                ("Battery", obd.commands.ELM_VOLTAGE),
                ("Coolant", obd.commands.COOLANT_TEMP),
                ("STFT1", getattr(obd.commands, "SHORT_FUEL_TRIM_1", None)),
                ("LTFT1", getattr(obd.commands, "LONG_FUEL_TRIM_1", None)),
                ("MAF", getattr(obd.commands, "MAF", None)),
                ("MAP", getattr(obd.commands, "MAP", None)),
                ("RPM", obd.commands.RPM),
                ("TPS", obd.commands.THROTTLE_POS),
            ]
            return self._snapshot_line("Sensor Health", items)
        if label == "Performance Snapshot":
            load_cmd = getattr(obd.commands, "ENGINE_LOAD", getattr(obd.commands, "CALCULATED_ENGINE_LOAD", None))
            items = [
                ("RPM", obd.commands.RPM),
                ("Speed", obd.commands.SPEED),
                ("Load", load_cmd),
                ("Throttle", obd.commands.THROTTLE_POS),
                ("IAT", obd.commands.INTAKE_TEMP),
            ]
            return self._snapshot_line("Performance snapshot", items)

        return f"{label}: not implemented."

    def _snapshot_line(self, label: str, items) -> str:
        parts = []
        for item_label, command in items:
            _, value = self._query_obd(command)
            parts.append(f"{item_label}={value}")
        return f"{label}: " + " | ".join(parts)

    @pyqtSlot(str, result=str)
    def runManualPid(self, pid):
        blocked = self._require_obd2("Manual PID")
        if blocked:
            return blocked
        text = str(pid or "").strip().replace(" ", "")
        if not text:
            return "Manual PID: enter hex command (e.g. 010C)."
        if any(c not in "0123456789abcdefABCDEF" for c in text):
            return "Manual PID: HEX only."
        try:
            import obd
            from Modules.Obd.obd_connection import safe_query
        except Exception as e:
            return f"Manual PID: query layer unavailable: {e}"
        try:
            raw = obd.OBDCommand(
                name=f"RAW_{text}",
                desc=f"custom PID {text}",
                command=text,
                decoder=lambda msgs: (msgs and str(msgs[0])) or "N/A",
                fast=False,
            )
            resp = safe_query(raw)
            return f"RAW {text}: {resp.value if resp and not resp.is_null() else 'N/A'}"
        except Exception as e:
            return f"Manual PID error: {e}"

    @pyqtSlot(bool)
    def setDiagnosticsVisible(self, visible):
        """Tell the telemetry hub whether the diagnostics page is on view, so
        it only polls diagnostics-only PIDs (Equiv. Ratio, Timing Adv, fuel
        rail pressures, intake/baro) while they're actually being looked at."""
        try:
            from Modules.Obd.telemetry import TelemetryHub
            TelemetryHub.instance().set_diagnostics_visible(bool(visible))
        except Exception:
            pass

    @pyqtSlot()
    def openSimulator(self):
        """Open the OBD Simulator window."""
        try:
            from Modules.Obd.obd_simulator import OBDSimulatorWindow
            if not hasattr(self, '_simulator_window') or self._simulator_window is None:
                sim = OBDSimulatorWindow(None)
                sim.setWindowTitle("OBD Simulator")
                sim.resize(980, 620)
                self._simulator_window = sim

                def _clear_ref():
                    self._simulator_window = None
                sim.destroyed.connect(_clear_ref)
            self._simulator_window.show()
            self._simulator_window.raise_()
            self._simulator_window.activateWindow()
        except Exception as e:
            print(f"WARNING: openSimulator failed: {e}")

    @pyqtSlot()
    def openEngineTest(self):
        """Open the Engine Test window."""
        try:
            from Modules.Obd.engine_test_window import EngineTestWindow
            if not hasattr(self, "_engine_test_window") or self._engine_test_window is None:
                win = EngineTestWindow(None)
                self._engine_test_window = win

                def _clear_ref():
                    self._engine_test_window = None
                win.destroyed.connect(_clear_ref)
            self._engine_test_window.show()
            self._engine_test_window.raise_()
            self._engine_test_window.activateWindow()
        except Exception as e:
            print(f"WARNING: openEngineTest failed: {e}")
