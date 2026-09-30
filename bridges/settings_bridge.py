from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot, QVariant
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QColorDialog

from preferences import (
    BASE_DIR,
    DATA_SETTINGS,
    read_base_rgb,
    read_style_color,
    read_style_str,
    read_style_int,
    read_style_bool,
    read_font_family,
    lighten_rgb,
    darken_rgb,
    BASE_COLOR_FALLBACK,
    FONT_GROUP_DEFAULTS,
    load_font_group,
    reload_typography,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TEXT_TERTIARY,
    TEXT_MUTED,
    COLOR_STATUS_GREEN,
    COLOR_STATUS_YELLOW,
    COLOR_STATUS_ORANGE,
    COLOR_STATUS_RED,
    COLOR_STATUS_NA,
    SURFACE_DARK,
    SURFACE_DIALOG,
)
from Modules.Runtime import settings_cache
from Modules.Runtime.wifi import WifiManager, WifiAdapterWatcher
from Modules.Runtime import services as _services


class _SystemInfoCache:
    """Background-refreshed snapshot of /proc, sysfs, and subprocess-derived
    system metrics. Every value the About page reads is fetched by a worker
    thread on a 2 s tick; the Qt main thread only ever does dict lookups.

    Without this, About-page pyqtSlots would call subprocess (vcgencmd, lspci,
    nmcli, rfkill, bluetoothctl — each up to 3 s timeout) and open /proc files
    synchronously on the Qt event loop, stalling the UI for tens to thousands
    of milliseconds whenever the About tab was visible."""

    REFRESH_INTERVAL_S = 2.0

    # Keys cached only once (values that never change at runtime).
    _STATIC_KEYS = ("gpu_name", "device_model")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._prev_proc_ticks = 0
        self._prev_wall_ticks = 0
        self._prev_idle_ticks = 0
        # Called on the worker thread after every refresh (the bridge hangs
        # a signal emit on it — Qt queues the delivery to the GUI thread).
        self.on_refresh = None
        # Seed once synchronously so the first QML read isn't "N/A".
        self._refresh(initial=True)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="SysInfoCache"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
        self._thread = None

    def get(self, key: str, default: str = "N/A") -> str:
        with self._lock:
            return self._values.get(key, default)

    def _loop(self) -> None:
        while not self._stop.wait(self.REFRESH_INTERVAL_S):
            try:
                self._refresh(initial=False)
            except Exception:
                pass

    def _refresh(self, initial: bool) -> None:
        new: dict[str, str] = {}

        # ── per-process CPU% (delta math; must run in this same thread to
        # keep the prev-tick counters coherent) ───────────────────────────
        try:
            pid = os.getpid()
            with open(f"/proc/{pid}/stat") as f:
                parts = f.read().split()
            proc_ticks = int(parts[13]) + int(parts[14])
            with open("/proc/stat") as f:
                cpu_parts = f.readline().split()
            wall_ticks = sum(int(v) for v in cpu_parts[1:9])
            idle_ticks = int(cpu_parts[4]) + int(cpu_parts[5])   # idle + iowait
            d_proc = proc_ticks - self._prev_proc_ticks
            d_wall = wall_ticks - self._prev_wall_ticks
            d_idle = idle_ticks - self._prev_idle_ticks
            self._prev_proc_ticks = proc_ticks
            self._prev_wall_ticks = wall_ticks
            self._prev_idle_ticks = idle_ticks
            ncpu = os.cpu_count() or 1
            if d_wall > 0 and not initial:
                pct = 100.0 * d_proc * ncpu / d_wall
                new["cpu_pct"] = f"{pct:.1f}%"
                sys_pct = 100.0 * (d_wall - d_idle) / d_wall
                new["sys_cpu_pct"] = f"{sys_pct:.0f}%"
                new["sys_cpu_pct_num"] = f"{sys_pct:.1f}"
            else:
                new["cpu_pct"] = "0.0%"
                new["sys_cpu_pct"] = "0%"
                new["sys_cpu_pct_num"] = "0"
        except Exception:
            new["cpu_pct"] = "N/A"
            new["sys_cpu_pct"] = "N/A"
            new["sys_cpu_pct_num"] = "-1"

        # ── /proc/<pid>/status (RSS + Threads in one read) ────────────────
        try:
            with open(f"/proc/{os.getpid()}/status") as f:
                rss_kb = None
                threads = None
                for line in f:
                    if line.startswith("VmRSS"):
                        rss_kb = int(line.split()[1])
                    elif line.startswith("Threads"):
                        threads = line.split()[1]
                    if rss_kb is not None and threads is not None:
                        break
            new["app_ram"] = f"{rss_kb / 1024:.1f} MB" if rss_kb is not None else "N/A"
            new["app_threads"] = threads or "N/A"
        except Exception:
            new["app_ram"] = "N/A"
            new["app_threads"] = "N/A"

        # ── /proc/meminfo (total/avail in one read) ───────────────────────
        try:
            with open("/proc/meminfo") as f:
                info = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        info[parts[0].rstrip(":")] = int(parts[1])
            total = info.get("MemTotal", 0)
            avail = info.get("MemAvailable", 0)
            used = total - avail
            new["ram_used"] = f"{used / 1024:.0f} MB"
            new["ram_free"] = f"{avail // 1024} MB"
            new["ram_avail_mb"] = f"{avail / 1024:.0f}"
            # CMA backs every GPU buffer under vc4-kms-v3d; exhaustion shows
            # up as allocation stalls (hitches). Only meaningful with a
            # Pi-sized pool (512 MB there) — an x86 box carries a few MB of
            # CMA nothing draws from, which must not read as "GPU starved".
            cma = info.get("CmaFree")
            cma_total = info.get("CmaTotal", 0)
            new["cma_free_mb"] = f"{cma / 1024:.0f}" if (cma is not None and cma_total >= 64 * 1024) else "-1"
        except Exception:
            new["ram_used"] = "N/A"
            new["ram_free"] = "N/A"
            new["ram_avail_mb"] = "-1"
            new["cma_free_mb"] = "-1"

        # ── /proc/uptime ──────────────────────────────────────────────────
        try:
            with open("/proc/uptime") as f:
                secs = int(float(f.read().split()[0]))
            h, rem = divmod(secs, 3600)
            m, s = divmod(rem, 60)
            new["uptime"] = f"{h}h {m}m {s}s"
        except Exception:
            new["uptime"] = "N/A"

        # ── CPU temp + load ───────────────────────────────────────────────
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                t_c = int(f.read().strip()) / 1000
            new["cpu_temp"] = f"{t_c:.1f} °C"
            new["cpu_temp_c"] = f"{t_c:.1f}"
        except Exception:
            new["cpu_temp"] = "N/A"
            new["cpu_temp_c"] = "-1"

        try:
            new["cpu_loadavg"] = f"{os.getloadavg()[0]:.1f}%"
        except Exception:
            new["cpu_loadavg"] = "N/A"

        # ── GPU usage + temp (sysfs first, vcgencmd subprocess fallback) ──
        new["gpu_usage"] = self._gpu_usage()
        new["gpu_temp"] = self._gpu_temp()
        new["gpu_load_pct"] = f"{self._gpu_load_pct(new['gpu_usage']):.0f}"

        # ── Pi power / throttling (sysfs, same word `vcgencmd get_throttled`
        # prints; no subprocess). The car's supply browns the Pi out — seen
        # 2026-09-15 as `hwmon: Undervoltage detected!` in 20-40 s stretches
        # while idle, get_throttled=0x50000 — which caps the ARM clock and,
        # on a bad enough dip, truncates whatever file was being written.
        # Logged on each transition so LaLaDash_ERROR.txt keeps the history
        # the (volatile) journal loses at reboot. ─────────────────────────
        flags = self._throttle_flags()
        new["power_flags"] = str(flags)
        new["power_status"] = self._power_text(flags)
        if flags >= 0:
            uv_now = bool(flags & 0x1)
            capped_now = bool(flags & (0x2 | 0x4))
            if uv_now != self._uv_active or capped_now != self._capped_active:
                stamp = time.strftime("%H:%M:%S")
                try:
                    from Modules.Runtime.trip_logger import log as _tl
                    if uv_now != self._uv_active:
                        _tl("power", "undervoltage", active=bool(uv_now), flags=int(flags))
                    if capped_now != self._capped_active:
                        _tl("power", "throttling", active=bool(capped_now), flags=int(flags))
                except Exception:
                    pass
                if uv_now != self._uv_active:
                    print(f"[power] {stamp} under-voltage {'began' if uv_now else 'ended'} (0x{flags:x})", flush=True)
                if capped_now != self._capped_active:
                    print(f"[power] {stamp} CPU throttling {'began' if capped_now else 'ended'} (0x{flags:x})", flush=True)
                self._uv_active = uv_now
                self._capped_active = capped_now

        # ── network: per-interface throughput + IPv4, no subprocess ──────
        # /proc/net/dev deltas against the previous tick; the ioctl for the
        # address. Keyed net_<iface>_* for every non-loopback interface that
        # is up, plus a "net_*" summary for the interface carrying the
        # default route (what the dock and the Connectivity page show).
        try:
            self._refresh_net(new, initial)
        except Exception:
            pass

        # ── nmcli wifi + rfkill bluetooth (slow subprocess) ───────────────
        new["wifi_enabled"] = "1" if self._wifi_enabled() else "0"
        # One nmcli call for both: the first active SSID (legacy key) and
        # the per-device map the two-adapter Connectivity page shows.
        by_dev = self._wifi_ssid_by_device()
        new["wifi_ssid"] = next((v for v in by_dev.values() if v), "")
        for dev in (new.get("net_ifaces_up") or "").split(","):
            if dev:
                new[f"net_{dev}_ssid"] = by_dev.get(dev, "")
        new["bluetooth_enabled"] = "1" if self._bt_enabled() else "0"
        new["bluetooth_device"] = self._bt_device()

        # ── Static values (read once, never re-fetched) ───────────────────
        if initial or "gpu_name" not in self._values:
            new["gpu_name"] = self._gpu_name()
        if initial or "device_model" not in self._values:
            try:
                with open("/proc/device-tree/model") as f:
                    new["device_model"] = f.read().strip().rstrip("\x00")
            except Exception:
                new["device_model"] = "Linux"

        with self._lock:
            self._values.update(new)
        cb = self.on_refresh
        if cb is not None and not initial:
            try:
                cb()
            except Exception:
                pass

    # ── Pi power ──────────────────────────────────────────────────────────
    # Some kernels expose the whole get_throttled word in sysfs; the car's
    # (6.12 rpt) does not, but its `rpi_volt` hwmon has the live
    # under-voltage alarm. So: sysfs alarm every tick (2 s), and the full
    # word — for the capped/throttled bits and the "occurred since boot"
    # history on the About page — from `vcgencmd get_throttled` (a 2 ms
    # mailbox call) every fifth tick, same as _gpu_temp() already resorts to.
    _THROTTLE_SYSFS = "/sys/devices/platform/soc/soc:firmware/get_throttled"
    _UV_ALARM_GLOB = "/sys/class/hwmon/hwmon*/in0_lcrit_alarm"
    _uv_active = False
    _capped_active = False
    _vc_flags: "int | None" = None
    _vc_tick = 0

    def _throttle_flags(self) -> int:
        """The firmware's get_throttled word, or -1 where there is none
        (dev PC). Bit 0 under-voltage now, 1 ARM frequency capped now,
        2 throttled now, 3 soft temperature limit now; bits 16-19 the same
        four "has occurred since boot"."""
        try:
            with open(self._THROTTLE_SYSFS) as f:
                return int(f.read().strip(), 16)
        except Exception:
            pass

        uv_now: "bool | None" = None
        try:
            import glob
            for path in glob.glob(self._UV_ALARM_GLOB):
                with open(path) as f:
                    uv_now = f.read().strip() == "1"
                break
        except Exception:
            uv_now = None

        self._vc_tick += 1
        if self._vc_flags is None or self._vc_tick % 5 == 1:
            try:
                out = subprocess.check_output(["vcgencmd", "get_throttled"], timeout=2).decode()
                self._vc_flags = int(out.strip().split("=")[-1], 16)
            except Exception:
                if self._vc_flags is None:
                    self._vc_flags = -1

        flags = self._vc_flags if self._vc_flags is not None else -1
        if uv_now is None:
            return flags
        if flags < 0:
            flags = 0
        flags = (flags & ~0x1) | (0x1 if uv_now else 0)
        if uv_now:
            flags |= 0x10000
        return flags

    @staticmethod
    def _power_text(flags: int) -> str:
        if flags < 0:
            return "N/A"
        now_bits = []
        if flags & 0x1:
            now_bits.append("under-voltage")
        if flags & 0x2:
            now_bits.append("frequency capped")
        if flags & 0x4:
            now_bits.append("throttled")
        if flags & 0x8:
            now_bits.append("soft temp limit")
        if now_bits:
            return "NOW: " + ", ".join(now_bits)
        past = []
        if flags & 0x10000:
            past.append("under-voltage")
        if flags & 0x40000:
            past.append("throttling")
        if flags & 0x80000:
            past.append("temp limit")
        if past:
            return "OK now — " + ", ".join(past) + " occurred since boot"
        return "OK"

    # ── GPU load ──────────────────────────────────────────────────────────
    # The Pi has no GPU-busy counter. A desktop GPU reports gpu_busy_percent
    # (a real percentage); on the Pi the only handle is the v3d clock, which
    # the firmware scales with load *on some firmware*. Treat the clock as a
    # load proxy only once it has been seen to vary (min < 80 % of max) —
    # a clock that never moves says nothing and must not raise an alert.
    _v3d_min_hz = 0.0
    _v3d_max_hz = 0.0

    def _gpu_load_pct(self, usage_text: str) -> float:
        txt = str(usage_text or "").strip()
        if txt.endswith("%"):
            try:
                return float(txt[:-1])
            except ValueError:
                return -1.0
        try:
            hz = float(txt)
        except ValueError:
            return -1.0
        if hz <= 0:
            return -1.0
        self._v3d_max_hz = max(self._v3d_max_hz, hz)
        self._v3d_min_hz = hz if self._v3d_min_hz <= 0 else min(self._v3d_min_hz, hz)
        if self._v3d_min_hz >= 0.8 * self._v3d_max_hz:
            return -1.0   # never seen it scale — unknown, not "100 %"
        return 100.0 * hz / self._v3d_max_hz

    # ── subprocess helpers (run only in the worker thread) ────────────────
    @staticmethod
    def _gpu_usage() -> str:
        try:
            import glob
            for card in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")):
                with open(card) as f:
                    return f"{int(f.read().strip())}%"
        except Exception:
            pass
        try:
            out = subprocess.check_output(
                ["vcgencmd", "measure_clock", "v3d"], timeout=2
            ).decode()
            return out.strip().split("=")[-1]
        except Exception:
            return "N/A"

    @staticmethod
    def _gpu_temp() -> str:
        try:
            import glob
            for hwmon in sorted(glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input")):
                with open(hwmon) as f:
                    return f"{int(f.read().strip()) / 1000:.1f} °C"
        except Exception:
            pass
        try:
            out = subprocess.check_output(
                ["vcgencmd", "measure_temp"], timeout=2
            ).decode()
            return out.strip().split("=")[1]
        except Exception:
            return "N/A"

    @staticmethod
    def _gpu_name() -> str:
        try:
            out = subprocess.check_output(["lspci", "-mm"], timeout=3).decode()
            for line in out.split("\n"):
                if "VGA" in line or "3D" in line or "Display" in line:
                    parts = line.split('"')
                    if len(parts) >= 6:
                        return f"{parts[3]} {parts[5]}"
        except Exception:
            pass
        return "N/A"

    # ── network helpers ──────────────────────────────────────────────────
    _net_prev: dict = {}
    _net_prev_t: float = 0.0

    @staticmethod
    def _default_route_iface() -> str:
        try:
            with open("/proc/net/route") as f:
                next(f)
                for line in f:
                    p = line.split()
                    if len(p) > 1 and p[1] == "00000000":
                        return p[0]
        except Exception:
            pass
        return ""

    @staticmethod
    def _ip4(iface: str) -> str:
        import fcntl
        import socket
        import struct
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = struct.pack("256s", iface[:15].encode())
            return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, packed)[20:24])  # SIOCGIFADDR
        except OSError:
            return ""
        finally:
            s.close()

    @staticmethod
    def _rate_text(bps: float) -> str:
        if bps >= 1_000_000:
            return f"{bps / 1_000_000:.1f} MB/s"
        if bps >= 1_000:
            return f"{bps / 1_000:.0f} KB/s"
        return f"{bps:.0f} B/s"

    def _refresh_net(self, new: dict, initial: bool) -> None:
        now = time.monotonic()
        dt = now - self._net_prev_t if self._net_prev_t else 0.0
        counters: dict = {}
        with open("/proc/net/dev") as f:
            for line in f:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                name = name.strip()
                if name == "lo" or name.startswith(("br-", "docker", "virbr", "veth")):
                    continue
                cols = rest.split()
                counters[name] = (int(cols[0]), int(cols[8]))   # rx bytes, tx bytes
        default = self._default_route_iface()
        new["net_default_iface"] = default
        ifaces_up = []
        for name, (rx, tx) in counters.items():
            ip = self._ip4(name)
            if not ip:
                continue
            ifaces_up.append(name)
            new[f"net_{name}_ip"] = ip
            prev = self._net_prev.get(name)
            if prev and dt > 0.2 and not initial:
                down = max(0.0, (rx - prev[0]) / dt)
                up = max(0.0, (tx - prev[1]) / dt)
            else:
                down = up = 0.0
            new[f"net_{name}_down_bps"] = f"{down:.0f}"
            new[f"net_{name}_up_bps"] = f"{up:.0f}"
            new[f"net_{name}_rate_text"] = f"↓ {self._rate_text(down)}  ↑ {self._rate_text(up)}"
        new["net_ifaces_up"] = ",".join(ifaces_up)
        self._net_prev = counters
        self._net_prev_t = now
        pick = default if default in ifaces_up else (ifaces_up[0] if ifaces_up else "")
        new["net_iface"] = pick
        new["net_ip"] = new.get(f"net_{pick}_ip", "") if pick else ""
        new["net_down_bps"] = new.get(f"net_{pick}_down_bps", "0") if pick else "0"
        new["net_up_bps"] = new.get(f"net_{pick}_up_bps", "0") if pick else "0"
        new["net_rate_text"] = new.get(f"net_{pick}_rate_text", "") if pick else ""

    @staticmethod
    def _wifi_ssid_by_device() -> dict:
        """{device: ssid} for every wifi device that is on a network, from
        nmcli's cached list (no rescan). Terse output escapes ':' and SSIDs
        contain them — _split_terse, never str.split."""
        from Modules.Runtime.wifi import _split_terse
        result = {}
        try:
            out = subprocess.check_output(
                ["nmcli", "-t", "-f", "ACTIVE,SSID,DEVICE", "dev", "wifi"], timeout=3
            ).decode()
        except Exception:
            return result
        for line in out.strip().split("\n"):
            parts = _split_terse(line)
            if len(parts) >= 3 and parts[0] == "yes":
                result[parts[2]] = parts[1]
        return result

    @staticmethod
    def _wifi_enabled() -> bool:
        try:
            out = subprocess.check_output(
                ["nmcli", "radio", "wifi"], timeout=3
            ).decode().strip()
            return out.lower() == "enabled"
        except Exception:
            return False


    @staticmethod
    def _bt_enabled() -> bool:
        try:
            out = subprocess.check_output(
                ["rfkill", "list", "bluetooth"], timeout=3
            ).decode()
            return "Soft blocked: no" in out
        except Exception:
            return False

    @staticmethod
    def _bt_device() -> str:
        try:
            out = subprocess.check_output(
                ["bluetoothctl", "info"], timeout=3
            ).decode()
            for line in out.split("\n"):
                if "Name:" in line:
                    return line.split("Name:", 1)[1].strip()
        except Exception:
            pass
        return ""


class SettingsBridge(QObject):
    """Exposes settings.ini values and theme colors to QML."""

    themeChanged = pyqtSignal()
    fullscreenChanged = pyqtSignal()
    settingsChanged = pyqtSignal()
    wifiChanged = pyqtSignal()
    # A second, occasional USB Wi-Fi adapter appearing/vanishing, and its own
    # scan/connect state — entirely separate from wifiChanged above so the
    # primary adapter's UI never re-evaluates on the secondary's account.
    wifi2Changed = pyqtSignal()
    # Network throughput / addresses, refreshed by the 2 s system-info worker.
    netChanged = pyqtSignal()
    # Settings → Services master switches (Modules/Runtime/services.py).
    servicesChanged = pyqtSignal()
    _services_rev: int = 0
    # Raspberry Pi package update (Settings → System → Update Raspberry Pi).
    piUpdateChanged = pyqtSignal()
    # Fired from the updater's worker thread; connected QueuedConnection so the
    # state lives on the Qt thread. See startPiUpdate().
    _piUpdateTick = pyqtSignal(float, str)
    _piUpdateLine = pyqtSignal(str)
    _piUpdateFinished = pyqtSignal(bool, str)

    _revision: int = 0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reload_colors()
        self._sysinfo = _SystemInfoCache()
        self._sysinfo.on_refresh = self.netChanged.emit
        self._sysinfo.start()

        # Wi-Fi. The manager owns a worker thread; nothing here touches nmcli
        # on the Qt main thread. No scan is started at boot — the Connectivity
        # page asks for one when it opens.
        self._wifi = WifiManager()
        self._wifi_available = WifiManager.available()
        self._wifi_networks: list = []
        self._wifi_busy = False
        self._wifi_status = "" if self._wifi_available else "nmcli not installed"

        # Second adapter (a longer-range USB dongle, plugged in only when
        # needed). self._wifi above — the primary/onboard path — is untouched
        # by any of this: _wifi2 is lazily created only once the watcher sees
        # a second wifi-type device, and torn down again the moment it's
        # unplugged. Two independent WifiManager instances means two
        # independent worker queues, so a slow connect on one adapter can
        # never stall a scan on the other.
        self._wifi2 = None  # WifiManager | None, created once a 2nd adapter is seen
        self._wifi2_networks: list = []
        self._wifi2_busy = False
        self._wifi2_status = ""
        self._wifi_watcher = WifiAdapterWatcher(self._on_wifi_adapter_change)
        if self._wifi_available:
            self._wifi_watcher.start()
        self._fullscreen_enabled = settings_cache.getboolean(
            "UI", "fullscreen", fallback=True, path=DATA_SETTINGS
        )
        self._home_widgets_enabled = settings_cache.getboolean(
            "UI", "home_widgets_enabled", fallback=True, path=DATA_SETTINGS
        )
        # Apply saved resolution override at startup (xrandr, non-blocking)
        saved_res = settings_cache.get(
            "UI", "resolution_override", fallback="native", path=DATA_SETTINGS
        ) or "native"
        if saved_res != "native":
            self._apply_resolution_override(saved_res)

    # ── internal ──────────────────────────────────────────────────────────
    def _reload_colors(self):
        self._base = read_base_rgb()
        r, g, b = self._base
        self._base_qcolor = QColor(r, g, b)
        self._opacity = read_style_int("opacity", fallback=180)

        for key in ("pressed_color", "secondary_color", "third_color",
                     "topbar_color", "top_color", "main_color",
                     "widgetcontainer_color", "extra_color",
                     "base_color_a", "base_color_b",
                     "secondary_color_a", "secondary_color_b",
                     "third_color_a", "third_color_b"):
            rgb = read_style_color(key, fallback=BASE_COLOR_FALLBACK)
            setattr(self, f"_{key}_rgb", rgb)

        self._glass_opacity = float(
            read_style_str("home_glass_opacity", fallback="0.07") or "0.07"
        )
        self._glass_enabled = read_style_bool("glass_effect_enabled", fallback=True)
        self._text_color_hex = read_style_str("text_color", fallback="#ffffff")
        self._progress_color_hex = read_style_str("progress_color", fallback="#2ec27e")
        self._font_family = read_font_family()

        # ── Pi update state (keeps running if the page is left) ───────────
        self._pi_updater = None
        self._pi_update_running = False
        self._pi_update_progress = 0.0
        self._pi_update_stage = ""
        self._pi_update_line = ""
        self._pi_update_result = ""      # "" while running, "ok", or an error
        from PyQt6.QtCore import Qt as _Qt
        self._piUpdateTick.connect(self._on_pi_update_tick, _Qt.ConnectionType.QueuedConnection)
        self._piUpdateLine.connect(self._on_pi_update_line, _Qt.ConnectionType.QueuedConnection)
        self._piUpdateFinished.connect(self._on_pi_update_finished, _Qt.ConnectionType.QueuedConnection)

    # ── base color ────────────────────────────────────────────────────────
    @pyqtProperty(QColor, notify=themeChanged)
    def baseColor(self):
        return self._base_qcolor

    @pyqtProperty(QColor, notify=themeChanged)
    def baseLightened(self):
        r, g, b = lighten_rgb(self._base, 20)
        return QColor(r, g, b)

    @pyqtProperty(QColor, notify=themeChanged)
    def baseDarkened(self):
        r, g, b = darken_rgb(self._base, 20)
        return QColor(r, g, b)

    @pyqtProperty(int, notify=themeChanged)
    def opacity(self):
        return self._opacity

    @pyqtProperty(float, notify=themeChanged)
    def glassOpacity(self):
        return self._glass_opacity

    @pyqtProperty(bool, notify=themeChanged)
    def glassEnabled(self):
        return self._glass_enabled

    @pyqtProperty(str, notify=themeChanged)
    def textColor(self):
        return self._text_color_hex

    @pyqtProperty(str, notify=themeChanged)
    def progressColor(self):
        return self._progress_color_hex

    @pyqtProperty(str, notify=themeChanged)
    def fontFamily(self):
        return self._font_family

    # ── named style colors ────────────────────────────────────────────────
    def _style_qcolor(self, attr):
        rgb = getattr(self, attr, (42, 47, 58))
        return QColor(*rgb)

    @pyqtProperty(QColor, notify=themeChanged)
    def pressedColor(self):
        return self._style_qcolor("_pressed_color_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def secondaryColor(self):
        return self._style_qcolor("_secondary_color_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def thirdColor(self):
        return self._style_qcolor("_third_color_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def topbarColor(self):
        return self._style_qcolor("_topbar_color_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def topColor(self):
        return self._style_qcolor("_top_color_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def mainColor(self):
        return self._style_qcolor("_main_color_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def widgetContainerColor(self):
        return self._style_qcolor("_widgetcontainer_color_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def extraColor(self):
        return self._style_qcolor("_extra_color_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def baseColorA(self):
        return self._style_qcolor("_base_color_a_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def baseColorB(self):
        return self._style_qcolor("_base_color_b_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def secondaryColorA(self):
        return self._style_qcolor("_secondary_color_a_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def secondaryColorB(self):
        return self._style_qcolor("_secondary_color_b_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def thirdColorA(self):
        return self._style_qcolor("_third_color_a_rgb")

    @pyqtProperty(QColor, notify=themeChanged)
    def thirdColorB(self):
        return self._style_qcolor("_third_color_b_rgb")

    # ── semantic color constants ──────────────────────────────────────────
    @pyqtProperty(str, constant=True)
    def textPrimary(self):
        return TEXT_PRIMARY

    @pyqtProperty(str, constant=True)
    def textSecondary(self):
        return TEXT_SECONDARY

    @pyqtProperty(str, constant=True)
    def textTertiary(self):
        return TEXT_TERTIARY

    @pyqtProperty(str, constant=True)
    def textMuted(self):
        return TEXT_MUTED

    @pyqtProperty(str, constant=True)
    def statusGreen(self):
        return COLOR_STATUS_GREEN

    @pyqtProperty(str, constant=True)
    def statusYellow(self):
        return COLOR_STATUS_YELLOW

    @pyqtProperty(str, constant=True)
    def statusOrange(self):
        return COLOR_STATUS_ORANGE

    @pyqtProperty(str, constant=True)
    def statusRed(self):
        return COLOR_STATUS_RED

    @pyqtProperty(str, constant=True)
    def statusNA(self):
        return COLOR_STATUS_NA

    @pyqtProperty(str, constant=True)
    def surfaceDark(self):
        return SURFACE_DARK

    @pyqtProperty(str, constant=True)
    def surfaceDialog(self):
        return SURFACE_DIALOG

    # ── font system ───────────────────────────────────────────────────────
    @pyqtSlot(str, result=QVariant)
    def fontGroup(self, group):
        """Return {size, weight, italic, spacing} for a named font group."""
        info = load_font_group(group)
        return {
            "size": info["size"],
            "weight": info["weight"],
            "italic": info["italic"],
            "spacing": info["spacing"],
        }

    # ── read / write settings ─────────────────────────────────────────────
    @pyqtSlot(str, str, result=str)
    def getValue(self, section, key):
        return settings_cache.get(section, key, fallback="", path=DATA_SETTINGS) or ""

    @pyqtSlot(str, str, result=bool)
    def getBool(self, section, key):
        return settings_cache.getboolean(section, key, fallback=False, path=DATA_SETTINGS)

    @pyqtSlot(str, str, result=int)
    def getInt(self, section, key):
        return settings_cache.getint(section, key, fallback=0, path=DATA_SETTINGS)

    @pyqtProperty(int, notify=settingsChanged)
    def revision(self):
        return SettingsBridge._revision

    @pyqtSlot(str, str, str)
    def setValue(self, section, key, value):
        settings_cache.set_value(section, key, value, path=DATA_SETTINGS)
        SettingsBridge._revision += 1
        self.settingsChanged.emit()

    # ── trips (odometer) ────────────────────────────────────────────────
    # trips_store.py is its own store (KM_STORE_PATH / KM_TOTAL_TXT_PATH),
    # deliberately outside Data/settings.ini so a dev-PC sync never clobbers
    # the car's real accumulated km — these two just forward into it.
    # Telemetry.totalKm/todayKm pick the new value up on the next telemetry
    # tick on their own (TelemetryBridge._maybe_update_trips), no extra
    # signal needed here.
    @pyqtSlot(float)
    def setTotalKm(self, value):
        from Modules.Settings.trips_store import set_total_km
        try:
            set_total_km(float(value))
        except (TypeError, ValueError):
            pass

    @pyqtSlot()
    def resetTodayKm(self):
        from Modules.Settings.trips_store import reset_today
        reset_today()

    @pyqtSlot()
    def reload(self):
        settings_cache.invalidate(DATA_SETTINGS)
        self._reload_colors()
        self._fullscreen_enabled = settings_cache.getboolean(
            "UI", "fullscreen", fallback=True, path=DATA_SETTINGS
        )
        self._home_widgets_enabled = settings_cache.getboolean(
            "UI", "home_widgets_enabled", fallback=True, path=DATA_SETTINGS
        )
        reload_typography()
        self.themeChanged.emit()
        self.fullscreenChanged.emit()

    # ── visual style ──────────────────────────────────────────────────────
    # The active style is read once at boot and applied by main.py. Changing it
    # only takes effect on the next start, so exactly one style is ever loaded.
    @pyqtProperty(str, notify=settingsChanged)
    def styleName(self):
        return settings_cache.get(
            "UI", "style", fallback="Modern", path=DATA_SETTINGS
        ) or "Modern"

    @pyqtProperty(list, constant=True)
    def availableStyles(self):
        from Modules.Runtime import styles
        return styles.names()

    @pyqtSlot(str)
    def setStyleName(self, name):
        """Persist the style choice. Takes effect after restartApp()."""
        from Modules.Runtime import styles
        canonical, _ = styles.resolve(name)
        self.setValue("UI", "style", canonical)

    @pyqtProperty(bool, notify=themeChanged)
    def homeWidgetsEnabled(self):
        return bool(self._home_widgets_enabled)

    @pyqtSlot(bool)
    def setHomeWidgetsEnabled(self, enabled):
        enabled = bool(enabled)
        self._home_widgets_enabled = enabled
        settings_cache.set_value(
            "UI", "home_widgets_enabled", "true" if enabled else "false", path=DATA_SETTINGS
        )
        self.themeChanged.emit()

    @pyqtProperty(bool, notify=fullscreenChanged)
    def fullscreenEnabled(self):
        return bool(self._fullscreen_enabled)

    def _apply_window_fullscreen(self, enabled: bool):
        app = QGuiApplication.instance()
        if app is None:
            return
        windows = list(app.topLevelWindows() or [])
        if not windows:
            return
        target = app.activeWindow() if app.activeWindow() in windows else windows[0]
        try:
            if enabled:
                target.showFullScreen()
            else:
                target.showNormal()
        except Exception:
            pass

    @pyqtSlot(bool)
    def setFullscreenEnabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self._fullscreen_enabled:
            self._apply_window_fullscreen(enabled)
            return
        self._fullscreen_enabled = enabled
        settings_cache.set_value(
            "UI", "fullscreen", "true" if enabled else "false", path=DATA_SETTINGS
        )
        self._apply_window_fullscreen(enabled)
        self.fullscreenChanged.emit()

    # ── resolution override ───────────────────────────────────────────────
    @pyqtProperty(str, notify=fullscreenChanged)
    def resolutionOverride(self):
        return settings_cache.get(
            "UI", "resolution_override", fallback="native", path=DATA_SETTINGS
        )

    @pyqtSlot(str)
    def setResolutionOverride(self, value):
        settings_cache.set_value("UI", "resolution_override", str(value), path=DATA_SETTINGS)
        self._apply_resolution_override(str(value))
        self.fullscreenChanged.emit()

    def _xrandr_output(self) -> str:
        """Return the name of the first connected xrandr output (e.g. 'HDMI-1')."""
        try:
            result = subprocess.run(
                ["xrandr"], capture_output=True, text=True, timeout=4
            )
            for line in result.stdout.splitlines():
                if " connected" in line:
                    return line.split()[0]
        except Exception:
            pass
        return ""

    # Default window size when the user picks "Native" while in windowed
    # mode. Matches the hard-coded ApplicationWindow geometry in main.qml.
    _DEFAULT_WINDOW_W = 1280
    _DEFAULT_WINDOW_H = 800

    def _apply_window_size(self, width: int, height: int) -> None:
        """Resize the top-level QML window. Fullscreen mode ignores this
        (the window automatically tracks screen size); we still call it so
        the saved geometry is correct when the user later exits fullscreen."""
        try:
            w = int(width); h = int(height)
        except Exception:
            return
        if w <= 0 or h <= 0:
            return
        app = QGuiApplication.instance()
        if app is None:
            return
        windows = list(app.topLevelWindows() or [])
        if not windows:
            return
        target = app.activeWindow() if app.activeWindow() in windows else windows[0]
        try:
            target.resize(w, h)
        except Exception:
            pass

    def _apply_resolution_override(self, value: str):
        output = self._xrandr_output()

        if value == "native":
            # Restore the screen to its EDID-preferred mode AND restore the
            # window to the QML default size in case it was shrunk earlier.
            if output:
                try:
                    subprocess.Popen(
                        ["xrandr", "--output", output, "--auto"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass
            if not self._fullscreen_enabled:
                self._apply_window_size(self._DEFAULT_WINDOW_W, self._DEFAULT_WINDOW_H)
            return

        try:
            w_str, h_str = value.lower().split("x")
            w_int = int(w_str); h_int = int(h_str)
        except Exception:
            return

        # Resize the QML window first — the bug we're fixing is that this
        # step never happened, so picking a smaller resolution never made
        # the app window smaller in windowed mode. Fullscreen apps follow
        # the screen automatically so the resize is harmless there too.
        if not self._fullscreen_enabled:
            self._apply_window_size(w_int, h_int)

        if not output:
            return  # No xrandr output detected — window resize alone is what we get.

        # Try the mode directly first
        try:
            result = subprocess.run(
                ["xrandr", "--output", output, "--mode", value],
                capture_output=True, timeout=4,
            )
            if result.returncode == 0:
                return
        except Exception:
            pass

        # Mode not in EDID — generate a custom CVT modeline and add it.
        # All xrandr invocations route stderr to DEVNULL because the X server
        # prints a BadName/RRCreateMode line when --newmode is rejected
        # (mode already registered, or compositor refuses dynamic modes), and
        # neither --addmode nor --mode have anything useful to say on stderr
        # in the failure path either.
        try:
            cvt = subprocess.run(
                ["cvt", w_str, h_str, "60"],
                capture_output=True, text=True, timeout=4,
            )
            for line in cvt.stdout.splitlines():
                if line.strip().startswith("Modeline"):
                    parts = line.strip().split(None, 1)[1]  # strip "Modeline "
                    mode_name = parts.split()[0].strip('"')
                    nm = subprocess.run(
                        ["xrandr", "--newmode"] + parts.split(),
                        timeout=4, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    # Treat exit 0 OR "BadName" (already exists) as "carry on
                    # and try --addmode"; any other failure means the X server
                    # won't let us add custom modes at all.
                    am = subprocess.run(
                        ["xrandr", "--addmode", output, mode_name],
                        timeout=4, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    if am.returncode != 0:
                        break
                    subprocess.Popen(
                        ["xrandr", "--output", output, "--mode", mode_name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    break
        except Exception:
            pass

    def applyStartupResolution(self):
        """Called once after QML loads to enforce any saved resolution
        override. We *always* call _apply_resolution_override now so the
        window size matches the saved value even when "native" — fixes the
        startup case where the user previously shrunk to 1280×720 and
        wanted that to persist."""
        override = self.resolutionOverride
        if override:
            self._apply_resolution_override(override)

    # ── system info (for About page) — values served from _SystemInfoCache,
    # which refreshes on a 2 s worker thread. These slots never block the
    # Qt event loop.
    @pyqtSlot(result=str)
    def cpuUsagePercent(self):
        return self._sysinfo.get("cpu_pct")

    @pyqtSlot(result=str)
    def appRamUsed(self):
        return self._sysinfo.get("app_ram")

    @pyqtSlot(result=str)
    def appThreads(self):
        return self._sysinfo.get("app_threads")

    @pyqtSlot(result=str)
    def gpuUsage(self):
        return self._sysinfo.get("gpu_usage")

    @pyqtSlot(result=str)
    def gpuTemp(self):
        return self._sysinfo.get("gpu_temp")

    @pyqtSlot(result=str)
    def gpuName(self):
        return self._sysinfo.get("gpu_name")

    @pyqtSlot(result=str)
    def cpuTemp(self):
        return self._sysinfo.get("cpu_temp")


    @pyqtSlot(result=str)
    def ramUsed(self):
        return self._sysinfo.get("ram_used")

    @pyqtSlot(result=str)
    def ramFree(self):
        return self._sysinfo.get("ram_free")

    @pyqtSlot(result=str)
    def uptime(self):
        return self._sysinfo.get("uptime")

    @pyqtSlot(result=str)
    def powerStatus(self):
        return self._sysinfo.get("power_status", "N/A")

    @pyqtSlot(result=str)
    def sysCpuUsage(self):
        return self._sysinfo.get("sys_cpu_pct", "N/A")

    @pyqtSlot(result=str)
    def gpuLoad(self):
        v = self._sysinfo.get("gpu_load_pct", "-1")
        try:
            return "N/A" if float(v) < 0 else f"{float(v):.0f}%"
        except ValueError:
            return "N/A"

    @pyqtSlot(result=str)
    def cmaFree(self):
        v = self._sysinfo.get("cma_free_mb", "-1")
        try:
            return "N/A" if float(v) < 0 else f"{float(v):.0f} MB"
        except ValueError:
            return "N/A"

    def pi_health(self) -> dict:
        """Numbers for the engine-state poll's Pi alerts (dict lookups on the
        2 s worker's cache; -1 = not available on this platform)."""
        def num(key):
            try:
                return float(self._sysinfo.get(key, "-1"))
            except (TypeError, ValueError):
                return -1.0
        return {
            "temp_c": num("cpu_temp_c"),
            "sys_cpu_pct": num("sys_cpu_pct_num"),
            "gpu_load_pct": num("gpu_load_pct"),
            "cma_free_mb": num("cma_free_mb"),
            "ram_avail_mb": num("ram_avail_mb"),
            "flags": self.pi_power_flags(),
        }

    def wifi_ssid_cached(self) -> str:
        """Current SSID from the 2 s worker (nmcli runs there, never here)."""
        return self._sysinfo.get("wifi_ssid", "") or ""

    def pi_power_flags(self) -> int:
        """Cached get_throttled word for the engine-state poll (dict lookup,
        refreshed by the 2 s worker); -1 where the platform has none."""
        try:
            return int(self._sysinfo.get("power_flags", "-1"))
        except (TypeError, ValueError):
            return -1

    @pyqtSlot(result=str)
    def deviceModel(self):
        return self._sysinfo.get("device_model", "Linux")

    # ── WiFi / Bluetooth toggles ────────────────────────────────────────
    # Reads come from _SystemInfoCache; writes stay fire-and-forget on a
    # daemon thread so the subprocess call never blocks the Qt event loop.
    @pyqtSlot(result=bool)
    def wifiEnabled(self):
        return self._sysinfo.get("wifi_enabled") == "1"

    @pyqtSlot(bool)
    def setWifiEnabled(self, on):
        self._wifi_status = "Turning Wi-Fi on…" if on else "Turning Wi-Fi off…"
        self.wifiChanged.emit()
        self._wifi.set_radio(bool(on), self._on_wifi_action)
        if on:
            # The radio needs a moment before it has anything to report.
            self._wifi.scan(self._on_wifi_scan, rescan=True)
        else:
            self._wifi_networks = []
            self.wifiChanged.emit()

    @pyqtSlot(result=str)
    def wifiSSID(self):
        return self._sysinfo.get("wifi_ssid", "")

    # ── WiFi network management ─────────────────────────────────────────
    # nmcli is a subprocess and a connect can take the better part of a
    # minute, so every one of these hands off to WifiManager's worker and
    # comes back through wifiChanged.
    @pyqtProperty(bool, constant=True)
    def wifiAvailable(self):
        return self._wifi_available

    @pyqtProperty(list, notify=wifiChanged)
    def wifiNetworks(self):
        return list(self._wifi_networks)

    @pyqtProperty(bool, notify=wifiChanged)
    def wifiBusy(self):
        return self._wifi_busy

    @pyqtProperty(str, notify=wifiChanged)
    def wifiStatus(self):
        return self._wifi_status

    @pyqtSlot()
    def scanWifi(self):
        if not self._wifi_available:
            self._wifi_status = "nmcli not installed"
            self.wifiChanged.emit()
            return
        self._wifi_busy = True
        self._wifi_status = "Scanning…"
        self.wifiChanged.emit()
        self._wifi.scan(self._on_wifi_scan, rescan=True)

    @pyqtSlot(str, str)
    def connectWifi(self, ssid, password):
        if not self._wifi_available:
            return
        self._wifi_busy = True
        self._wifi_status = f"Connecting to {ssid}…"
        self.wifiChanged.emit()
        self._wifi.connect(str(ssid), str(password), self._on_wifi_action)

    @pyqtSlot(str)
    def disconnectWifi(self, ssid):
        if not self._wifi_available:
            return
        self._wifi_busy = True
        self._wifi_status = "Disconnecting…"
        self.wifiChanged.emit()
        self._wifi.disconnect(str(ssid), self._on_wifi_action)

    @pyqtSlot(str)
    def forgetWifi(self, ssid):
        if not self._wifi_available:
            return
        self._wifi_busy = True
        self._wifi_status = f"Forgetting {ssid}…"
        self.wifiChanged.emit()
        self._wifi.forget(str(ssid), self._on_wifi_action)


    # Callbacks below run on the WifiManager worker. Rebinding an attribute and
    # emitting is safe from another thread — Qt queues the signal to the GUI
    # thread — and mirrors what _SystemInfoCache already does here.
    def _on_wifi_scan(self, rows):
        self._wifi_networks = list(rows or [])
        self._wifi_busy = False
        if not self._wifi_networks:
            self._wifi_status = "No networks found"
        elif self._wifi_status in ("Scanning…", ""):
            self._wifi_status = f"{len(self._wifi_networks)} networks"
        self.wifiChanged.emit()

    def _on_wifi_action(self, ok, message):
        self._wifi_busy = False
        self._wifi_status = str(message or ("Done" if ok else "Failed"))
        self.wifiChanged.emit()
        # The scan's active/saved flags are stale the moment a connect lands.
        self._wifi.scan(self._on_wifi_scan, rescan=False)

    # ── second Wi-Fi adapter (long-range USB dongle) ─────────────────────
    # Mirrors the primary block above property-for-property, routed through
    # self._wifi2 instead of self._wifi. The primary block is never touched
    # by any of this — see the comment on self._wifi2's declaration.
    def _on_wifi_adapter_change(self):
        """Runs on WifiAdapterWatcher's own thread. Rebinding attributes and
        emitting is safe cross-thread — Qt queues the signal delivery to the
        GUI thread — the same pattern _on_wifi_scan/_on_wifi_action already
        rely on for WifiManager's worker thread."""
        iface = self._wifi_watcher.secondary_iface
        # With two adapters, an unpinned `nmcli dev wifi list` merges both
        # radios' scans and `connection up` lands on whichever device nmcli
        # fancies — which is how tapping the onboard Wi-Fi ended up driving
        # the USB dongle. Pin the primary to its own device for as long as a
        # second one is present.
        self._wifi.set_iface(self._wifi_watcher.primary_iface if iface else None)
        if iface and (self._wifi2 is None or self._wifi2.iface != iface):
            self._wifi2 = WifiManager(iface=iface)
            self._wifi2_networks = []
            self._wifi2_busy = False
            self._wifi2_status = ""
            self._wifi2.scan(self._on_wifi2_scan, rescan=True)
            self._wifi.scan(self._on_wifi_scan, rescan=False)   # re-list, now pinned
        elif not iface and self._wifi2 is not None:
            self._wifi2.stop()
            self._wifi2 = None
            self._wifi2_networks = []
            self._wifi2_busy = False
            self._wifi2_status = ""
        self.wifi2Changed.emit()

    @pyqtProperty(bool, notify=wifi2Changed)
    def hasSecondWifiAdapter(self):
        return self._wifi2 is not None

    @pyqtProperty(str, notify=wifi2Changed)
    def secondWifiAdapterName(self):
        return self._wifi2.iface if self._wifi2 is not None else ""

    # ── network readouts (throughput + addresses) ────────────────────────
    # All from _SystemInfoCache: dict lookups here, /proc + one ioctl on the
    # worker. `netRateText` is what the dock shows while parked.
    @pyqtProperty(str, notify=netChanged)
    def netRateText(self):
        return self._sysinfo.get("net_rate_text", "") or ""

    @pyqtProperty(str, notify=netChanged)
    def netIp(self):
        return self._sysinfo.get("net_ip", "") or ""

    @pyqtProperty(str, notify=netChanged)
    def netIface(self):
        return self._sysinfo.get("net_iface", "") or ""


    @pyqtSlot(str, result="QVariant")
    def netInfo(self, iface):
        """Per-interface readout for the Connectivity page / Wi-Fi sheet:
        {ip, ssid, rateText, down, up, up_now, isDefault}."""
        g = self._sysinfo.get
        iface = str(iface or "")
        up_list = (g("net_ifaces_up", "") or "").split(",")
        return {
            "iface": iface,
            "ip": g(f"net_{iface}_ip", "") or "",
            "ssid": g(f"net_{iface}_ssid", "") or "",
            "rateText": g(f"net_{iface}_rate_text", "") or "",
            "down": g(f"net_{iface}_down_bps", "0") or "0",
            "up": g(f"net_{iface}_up_bps", "0") or "0",
            "isUp": iface in up_list,
            "isDefault": iface == (g("net_default_iface", "") or ""),
        }

    @pyqtProperty(str, notify=wifi2Changed)
    def wifiAdapterName(self):
        """The onboard adapter's device name (wlan0 on the Pi)."""
        # From the watcher's own thread — never nmcli here, this is a property.
        w = getattr(self, "_wifi_watcher", None)
        return (w.primary_iface if w is not None else None) or ""

    @pyqtProperty(list, notify=wifi2Changed)
    def wifi2Networks(self):
        return list(self._wifi2_networks)

    @pyqtProperty(bool, notify=wifi2Changed)
    def wifi2Busy(self):
        return self._wifi2_busy

    @pyqtProperty(str, notify=wifi2Changed)
    def wifi2Status(self):
        return self._wifi2_status

    @pyqtSlot()
    def scanWifi2(self):
        if self._wifi2 is None:
            return
        self._wifi2_busy = True
        self._wifi2_status = "Scanning…"
        self.wifi2Changed.emit()
        self._wifi2.scan(self._on_wifi2_scan, rescan=True)

    @pyqtSlot(str, str)
    def connectWifi2(self, ssid, password):
        if self._wifi2 is None:
            return
        self._wifi2_busy = True
        self._wifi2_status = f"Connecting to {ssid}…"
        self.wifi2Changed.emit()
        self._wifi2.connect(str(ssid), str(password), self._on_wifi2_action)

    @pyqtSlot(str)
    def disconnectWifi2(self, ssid):
        if self._wifi2 is None:
            return
        self._wifi2_busy = True
        self._wifi2_status = "Disconnecting…"
        self.wifi2Changed.emit()
        self._wifi2.disconnect(str(ssid), self._on_wifi2_action)

    @pyqtSlot(str)
    def forgetWifi2(self, ssid):
        if self._wifi2 is None:
            return
        self._wifi2_busy = True
        self._wifi2_status = f"Forgetting {ssid}…"
        self.wifi2Changed.emit()
        self._wifi2.forget(str(ssid), self._on_wifi2_action)


    def _on_wifi2_scan(self, rows):
        if self._wifi2 is None:
            return  # adapter was unplugged while the scan was in flight
        self._wifi2_networks = list(rows or [])
        self._wifi2_busy = False
        if not self._wifi2_networks:
            self._wifi2_status = "No networks found"
        elif self._wifi2_status in ("Scanning…", ""):
            self._wifi2_status = f"{len(self._wifi2_networks)} networks"
        self.wifi2Changed.emit()

    def _on_wifi2_action(self, ok, message):
        if self._wifi2 is None:
            return
        self._wifi2_busy = False
        self._wifi2_status = str(message or ("Done" if ok else "Failed"))
        self.wifi2Changed.emit()
        self._wifi2.scan(self._on_wifi2_scan, rescan=False)

    @pyqtSlot(result=bool)
    def bluetoothEnabled(self):
        return self._sysinfo.get("bluetooth_enabled") == "1"

    @pyqtSlot(bool)
    def setBluetoothEnabled(self, on):
        threading.Thread(
            target=lambda: subprocess.run(
                ["rfkill", "unblock" if on else "block", "bluetooth"], timeout=5
            ),
            daemon=True,
        ).start()

    @pyqtSlot(result=str)
    def bluetoothDevice(self):
        return self._sysinfo.get("bluetooth_device", "")

    # ── system actions ──────────────────────────────────────────────────
    @pyqtSlot()
    def restartAudio(self):
        def _restart():
            restarted = False
            try:
                subprocess.run(["systemctl", "--user", "restart", "pipewire"], timeout=10, check=False)
                subprocess.run(["systemctl", "--user", "restart", "pipewire-pulse"], timeout=10, check=False)
                subprocess.run(["systemctl", "--user", "restart", "wireplumber"], timeout=10, check=False)
                restarted = True
            except Exception:
                try:
                    subprocess.run(["pulseaudio", "-k"], timeout=5, check=False)
                    subprocess.run(["pulseaudio", "--start"], timeout=5, check=False)
                    restarted = True
                except Exception:
                    pass
            try:
                from Modules.Music.audio_daemon_mgr import stop as audio_daemon_stop
                from Modules.Music.audio_daemon_mgr import ensure_started as audio_daemon_ensure_started
                audio_daemon_stop()
                audio_daemon_ensure_started()
                restarted = True
            except Exception:
                pass
            if restarted:
                try:
                    mb = self._music_bridge()
                    if mb is not None and hasattr(mb, "refreshAudioPlayers"):
                        mb.refreshAudioPlayers()
                except Exception:
                    pass
        threading.Thread(target=_restart, daemon=True).start()

    def _music_bridge(self):
        app = QGuiApplication.instance()
        if app is None:
            return None
        bridges = getattr(app, "_bridges", None)
        if not isinstance(bridges, dict):
            return None
        return bridges.get("music")

    @pyqtSlot()
    def restartMprisBridge(self):
        def _restart():
            mb = self._music_bridge()
            if mb is None:
                return
            app = QGuiApplication.instance()
            mpris = getattr(app, "_mpris", None) if app is not None else None
            if mpris is not None:
                try:
                    mpris.stop()
                except Exception:
                    pass
                time.sleep(0.25)
                try:
                    mpris.start()
                    if app is not None:
                        app._mpris = mpris
                    return
                except Exception:
                    pass
            try:
                from Modules.Music.mpris_bridge import MPRISBridge
            except Exception:
                return
            try:
                controller_factory = getattr(mb, "create_mpris_controller", None)
                controller = controller_factory() if callable(controller_factory) else None
                if controller is None:
                    player = getattr(mb, "_player", None)
                    controller_fn = getattr(player, "_controller_for_mpris", None)
                    controller = controller_fn() if callable(controller_fn) else None
                if controller is None:
                    return
                bridge = MPRISBridge(controller=controller, identity="LaLaDash")
                bridge.start()
                if app is not None:
                    app._mpris = bridge
            except Exception:
                pass
        threading.Thread(target=_restart, daemon=True).start()

    @pyqtSlot(result=str)
    def mprisCommandLog(self):
        path = os.path.join(os.path.dirname(os.path.abspath(DATA_SETTINGS)), "mpris_commands.log")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-80:]
            return "".join(lines).strip()
        except Exception:
            return ""

    @pyqtSlot()
    def clearCaches(self):
        def _clear():
            cache_paths = [
                os.path.join("Data", "art_cache"),
                os.path.join("Data", "sounddrive_cache"),
                os.path.join("Data", ".cache"),
            ]
            for path in cache_paths:
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                except Exception:
                    pass
            try:
                from Modules.Runtime import asset_cache
                asset_cache.invalidate()
            except Exception:
                pass
            try:
                from Modules.Runtime import catalog_cache
                catalog_cache.invalidate()
            except Exception:
                pass
            try:
                mb = self._music_bridge()
                player = getattr(mb, "_player", None) if mb is not None else None
                art_cache = getattr(player, "_art_cache", None)
                if isinstance(art_cache, dict):
                    art_cache.clear()
            except Exception:
                pass
        threading.Thread(target=_clear, daemon=True).start()

    @pyqtSlot()
    def rebootSystem(self):
        try:
            subprocess.Popen(["sudo", "reboot"])
        except Exception:
            pass

    def _save_music_session(self):
        mb = self._music_bridge()
        if mb is not None:
            try:
                mb.saveSession()
            except Exception:
                pass

    @pyqtSlot()
    def shutdownSystem(self):
        self._save_music_session()
        try:
            subprocess.Popen(["sudo", "poweroff"])
        except Exception:
            pass

    # ── app lifecycle ────────────────────────────────────────────────────
    # On the car the app is a systemd user unit (laladash.service). Anything
    # we spawn lives in that unit's cgroup and dies with it when the unit
    # stops — which is why the old restart (Popen a new main.py, quit) went
    # to a black screen: the child was killed along with us. Under systemd,
    # restarting means asking systemd; leaving for the desktop means starting
    # the desktop in a unit of its own.
    @staticmethod
    def _under_systemd() -> bool:
        return bool(os.environ.get("INVOCATION_ID")) and shutil.which("systemctl") is not None

    _SERVICE = "laladash.service"

    # ── Services: the extras' master switches ────────────────────────────
    @pyqtProperty(int, notify=servicesChanged)
    def servicesRev(self):
        """Bumped on every change so `serviceOn()` bindings re-run."""
        return self._services_rev

    @pyqtSlot(str, result=bool)
    def serviceOn(self, name):
        return _services.service_on(str(name))


    @pyqtSlot(str, bool)
    def setServiceOn(self, name, on):
        _services.set_service(str(name), bool(on))
        self._services_rev += 1
        self.servicesChanged.emit()

    # ── Raspberry Pi update (apt update → full-upgrade → autoclean) ──────
    @pyqtProperty(bool, notify=piUpdateChanged)
    def piUpdateAvailable(self):
        from Modules.Runtime import pi_update
        return pi_update.available()

    @pyqtProperty(bool, notify=piUpdateChanged)
    def piUpdateRunning(self):
        return self._pi_update_running

    @pyqtProperty(float, notify=piUpdateChanged)
    def piUpdateProgress(self):
        return self._pi_update_progress

    @pyqtProperty(str, notify=piUpdateChanged)
    def piUpdateStage(self):
        return self._pi_update_stage

    @pyqtProperty(str, notify=piUpdateChanged)
    def piUpdateLine(self):
        return self._pi_update_line

    @pyqtProperty(str, notify=piUpdateChanged)
    def piUpdateResult(self):
        """"" while idle/running, "ok" on success, else the error text."""
        return self._pi_update_result

    @pyqtSlot()
    def startPiUpdate(self):
        if self._pi_update_running:
            return
        from Modules.Runtime.pi_update import PiUpdater
        self._pi_update_running = True
        self._pi_update_progress = 0.0
        self._pi_update_stage = "Starting…"
        self._pi_update_line = ""
        self._pi_update_result = ""
        self.piUpdateChanged.emit()
        self._pi_updater = PiUpdater(
            self._piUpdateTick.emit, self._piUpdateLine.emit, self._piUpdateFinished.emit)
        self._pi_updater.start()

    @pyqtSlot()
    def dismissPiUpdate(self):
        """Clear a finished run's banner (no effect while it runs)."""
        if self._pi_update_running:
            return
        self._pi_update_stage = ""
        self._pi_update_line = ""
        self._pi_update_result = ""
        self._pi_update_progress = 0.0
        self.piUpdateChanged.emit()

    def _on_pi_update_tick(self, frac, stage):
        # Monotonic: a later stage's status stream can briefly report a small
        # percent, but the base offset keeps the bar from going backwards.
        self._pi_update_progress = max(self._pi_update_progress, float(frac))
        if stage:
            self._pi_update_stage = stage
        self.piUpdateChanged.emit()

    def _on_pi_update_line(self, line):
        self._pi_update_line = line
        self.piUpdateChanged.emit()

    def _on_pi_update_finished(self, ok, message):
        self._pi_update_running = False
        if ok:
            self._pi_update_progress = 1.0
        self._pi_update_stage = "Done" if ok else "Failed"
        self._pi_update_result = "ok" if ok else (message or "Update failed")
        self._pi_update_line = message
        self.piUpdateChanged.emit()

    @pyqtSlot()
    def restartApp(self):
        self._save_music_session()
        if self._under_systemd():
            # --no-block: the SIGTERM this sends us must not wait on us.
            try:
                subprocess.Popen(["systemctl", "--user", "--no-block", "restart", self._SERVICE])
                return
            except Exception:
                pass
        try:
            subprocess.Popen([sys.executable] + sys.argv)
        except Exception:
            pass
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            app.quit()

    @pyqtSlot()
    def quitApp(self):
        self._save_music_session()
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            app.quit()

    @pyqtSlot()
    def quitToDesktop(self):
        """Quit and bring the Pi desktop up in our place
        (Installation/pi-config/desktop_now.sh). The script runs in a
        transient unit of its own so it outlives ours; it stops
        laladash.service itself, which is what ends this process."""
        self._save_music_session()
        script = os.path.join(BASE_DIR, "Installation", "pi-config", "desktop_now.sh")
        env = [f"--setenv={k}={os.environ[k]}" for k in
               ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS")
               if os.environ.get(k)]
        if self._under_systemd() and shutil.which("systemd-run"):
            try:
                subprocess.Popen(["systemd-run", "--user", "--collect",
                                  "--unit", f"laladash-desktop-{int(time.time())}",
                                  *env, "/bin/bash", script])
                return
            except Exception:
                pass
        # Not under systemd (dev PC): nothing to bring back, just quit.
        self.quitApp()

    # ── Dash-light simulator (Settings > Debugger > Lights Simulator) ─────
    # Overrides live in DashLightsHub (Auto / Force ON / Force OFF per key)
    # and take effect everywhere the effective set is drawn — the Dashboard
    # box, the driving screensaver's strip, the light → alert mapping. The
    # effective set itself is Telemetry.activeDashLightKeys; this only owns
    # the overrides. The old QWidget dialog this replaces opened as a second
    # X window over the app, which under labwc/Xwayland was untouchable.
    dashLightOverridesChanged = pyqtSignal()

    @pyqtProperty(list, constant=True)
    def dashLightCatalog(self):
        from Modules.Layout.dash_lights_hub import DASH_LIGHT_CATALOG
        return [{"key": k, "label": lbl, "group": grp} for k, lbl, grp in DASH_LIGHT_CATALOG]

    @pyqtProperty(list, notify=dashLightOverridesChanged)
    def dashLightOverrideKeys(self):
        """Keys with any override set (ON or OFF)."""
        from Modules.Layout.dash_lights_hub import DashLightsHub
        return sorted(DashLightsHub.instance().get_overrides().keys())

    @pyqtSlot(str, result=int)
    def dashLightOverride(self, key):
        """-1 = Auto (no override), 0 = forced off, 1 = forced on."""
        from Modules.Layout.dash_lights_hub import DashLightsHub
        st = DashLightsHub.instance().get_override(str(key))
        return -1 if st is None else (1 if st else 0)

    @pyqtSlot(str, int)
    def setDashLightOverride(self, key, state):
        from Modules.Layout.dash_lights_hub import DashLightsHub
        st = None if int(state) < 0 else bool(int(state))
        DashLightsHub.instance().set_override(str(key), st)
        self.dashLightOverridesChanged.emit()

    @pyqtSlot(list, int)
    def setDashLightOverrides(self, keys, state):
        """Same as setDashLightOverride for a list of keys."""
        from Modules.Layout.dash_lights_hub import DashLightsHub
        hub = DashLightsHub.instance()
        st = None if int(state) < 0 else bool(int(state))
        for key in keys or []:
            hub.set_override(str(key), st)
        self.dashLightOverridesChanged.emit()

    @pyqtSlot(int)
    def setAllDashLights(self, state):
        """Force every catalog light ON (1) / OFF (0), or Auto (-1)."""
        from Modules.Layout.dash_lights_hub import DASH_LIGHT_CATALOG
        self.setDashLightOverrides([k for k, _, _ in DASH_LIGHT_CATALOG], state)

    @pyqtSlot(str)
    def applyDashLightPreset(self, name):
        """Force the preset's lights ON and put every other override back
        to Auto, so presets replace each other rather than piling up."""
        from Modules.Layout.dash_lights_hub import DashLightsHub, DASH_LIGHT_PRESETS
        keys = DASH_LIGHT_PRESETS.get(str(name), [])
        hub = DashLightsHub.instance()
        hub.clear_overrides()
        for key in keys:
            hub.set_override(key, True)
        self.dashLightOverridesChanged.emit()

    @pyqtSlot()
    def clearDashLightOverrides(self):
        from Modules.Layout.dash_lights_hub import DashLightsHub
        DashLightsHub.instance().clear_overrides()
        self.dashLightOverridesChanged.emit()

    @pyqtSlot(result=list)
    def dashLightsDetected(self):
        """What the real sources (camera detector, CAN map) currently report,
        before overrides — so the simulator can show what the car itself says."""
        from Modules.Layout.dash_lights_hub import DashLightsHub
        hub = DashLightsHub.instance()
        seen = []
        for k in hub.get_detected_active() + hub.get_can_active():
            if k not in seen:
                seen.append(k)
        return seen


    @pyqtSlot(str, str, str, result=str)
    def getString(self, section: str, key: str, default: str = "") -> str:
        """Alias for getValue with an optional default."""
        return settings_cache.get(section, key, fallback=default, path=DATA_SETTINGS) or default

    @pyqtSlot(str, str, str)
    def setString(self, section: str, key: str, value: str) -> None:
        """Write a settings key and bump revision."""
        settings_cache.set_value(section, key, str(value), path=DATA_SETTINGS)
        SettingsBridge._revision += 1
        self.settingsChanged.emit()

    # ── Color picking ────────────────────────────────────────────────
    @pyqtSlot(str, result=str)
    def pickColor(self, initial_hex):
        """Open a native color dialog. Returns selected hex or empty string if cancelled."""
        initial = QColor(initial_hex) if initial_hex else QColor(42, 47, 58)
        chosen = QColorDialog.getColor(initial, None, "Pick Color")
        if chosen.isValid():
            return chosen.name()
        return ""

    @pyqtSlot(str, str)
    def setStyleColor(self, key, hex_color):
        """Write a single [Style] color key and reload theme."""
        settings_cache.set_value("Style", key, hex_color, path=DATA_SETTINGS)
        settings_cache.invalidate(DATA_SETTINGS)
        self._reload_colors()
        reload_typography()
        self.themeChanged.emit()

    @staticmethod
    def _blend_hex(hex_a, hex_b):
        """Blend two hex colors by averaging RGB (matches old _blend_hex)."""
        ca = QColor(hex_a)
        cb = QColor(hex_b)
        r = (ca.red() + cb.red()) // 2
        g = (ca.green() + cb.green()) // 2
        b = (ca.blue() + cb.blue()) // 2
        return QColor(r, g, b).name()

    # Color group definitions (Settings → Themes colour editor)
    _COLOR_GROUPS = {
        "base_color_a": {
            "pair": "base_color_b",
            "outputs": ["base_color", "main_color"],
        },
        "base_color_b": {
            "pair": "base_color_a",
            "outputs": ["base_color", "main_color"],
        },
        "secondary_color_a": {
            "pair": "secondary_color_b",
            "outputs": ["secondary_color", "widgetcontainer_color"],
        },
        "secondary_color_b": {
            "pair": "secondary_color_a",
            "outputs": ["secondary_color", "widgetcontainer_color"],
        },
        "third_color_a": {
            "pair": "third_color_b",
            "outputs": ["third_color", "extra_color"],
        },
        "third_color_b": {
            "pair": "third_color_a",
            "outputs": ["third_color", "extra_color"],
        },
    }

    @pyqtSlot(str, str, str, str)
    def applyColorGroup(self, key_a, key_b, hex_a, hex_b):
        """Write Color A + Color B, compute blend, write outputs, reload."""
        settings_cache.set_value("Style", key_a, hex_a, path=DATA_SETTINGS)
        settings_cache.set_value("Style", key_b, hex_b, path=DATA_SETTINGS)

        blended = self._blend_hex(hex_a, hex_b)

        # Determine output keys from group definition
        group_info = self._COLOR_GROUPS.get(key_a)
        if group_info:
            for out_key in group_info["outputs"]:
                settings_cache.set_value("Style", out_key, blended, path=DATA_SETTINGS)

        # If this is the primary group, also apply derived colors
        if key_a == "base_color_a":
            self._apply_primary_offsets(blended)

        settings_cache.invalidate(DATA_SETTINGS)
        self._reload_colors()
        reload_typography()
        self.themeChanged.emit()

    # ── Font group raw data for QML editors ──────────────────────────
    @pyqtSlot(str, result=QVariant)
    def fontGroupRaw(self, group):
        """Return raw {size, spacing, style, debug} for a font group editor."""
        prefix = str(group or "").strip().lower().replace(" ", "_")
        defaults = FONT_GROUP_DEFAULTS.get(group, {"size": 14, "spacing": 0.0, "style": "regular"})
        try:
            size = int(settings_cache.get("FontSettings", f"{prefix}_size",
                       fallback=str(defaults.get("size", 14)), path=DATA_SETTINGS) or defaults.get("size", 14))
            size = max(8, min(64, size))
        except (TypeError, ValueError):
            size = int(defaults.get("size", 14))
        try:
            spacing = float(settings_cache.get("FontSettings", f"{prefix}_spacing",
                            fallback=str(defaults.get("spacing", 0.0)), path=DATA_SETTINGS) or defaults.get("spacing", 0.0))
            spacing = max(-2.0, min(12.0, spacing))
        except (TypeError, ValueError):
            spacing = float(defaults.get("spacing", 0.0))
        style = settings_cache.get("FontSettings", f"{prefix}_style",
                fallback=defaults.get("style", "regular"), path=DATA_SETTINGS) or "regular"
        raw_dbg = settings_cache.get("FontSettings", f"{prefix}_debug",
                  fallback="0", path=DATA_SETTINGS) or "0"
        debug = raw_dbg.strip().lower() in ("1", "true", "yes", "on")
        return {"size": size, "spacing": spacing, "style": style.strip().lower(), "debug": debug}

    @pyqtSlot(str, str, str)
    def setFontValue(self, group, field, value):
        """Write a single font field (size|spacing|style|debug) and reload."""
        prefix = str(group or "").strip().lower().replace(" ", "_")
        settings_cache.set_value("FontSettings", f"{prefix}_{field}", value, path=DATA_SETTINGS)
        settings_cache.invalidate(DATA_SETTINGS)
        reload_typography()
        self.themeChanged.emit()

    def _apply_primary_offsets(self, base_hex):
        """Derive topbar_color and top_color from base_color (matches old logic)."""
        from preferences import darken_rgb
        c = QColor(base_hex)
        rgb = (c.red(), c.green(), c.blue())
        darkened = darken_rgb(rgb, 8)
        dark_hex = QColor(*darkened).name()

        # Only auto-set if auto flags are enabled
        auto_topbar = settings_cache.get("Style", "auto_topbar_color", fallback="true", path=DATA_SETTINGS)
        if auto_topbar and auto_topbar.lower() in ("true", "1", "yes"):
            settings_cache.set_value("Style", "topbar_color", dark_hex, path=DATA_SETTINGS)

        auto_top = settings_cache.get("Style", "auto_top_color", fallback="true", path=DATA_SETTINGS)
        if auto_top and auto_top.lower() in ("true", "1", "yes"):
            settings_cache.set_value("Style", "top_color", dark_hex, path=DATA_SETTINGS)
