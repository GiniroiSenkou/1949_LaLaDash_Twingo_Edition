import shutil
import subprocess
import json
import re
import time
import threading
from typing import List, Dict, Tuple, Optional
from subprocess import run, PIPE

def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = run(cmd, stdout=PIPE, stderr=PIPE, text=True)
    return p.returncode, p.stdout, p.stderr

def get_default_sink_name() -> str | None:
    rc, out, _ = _run(["pactl", "info"])
    if rc != 0:
        return None
    m = re.search(r"Default Sink:\s*(.+)", out)
    return m.group(1).strip() if m else None

def list_sinks_short() -> dict[str, int]:
    """Return {sink_name: index} from `pactl list short sinks`."""
    rc, out, _ = _run(["pactl", "list", "short", "sinks"])
    if rc != 0:
        return {}
    mapping = {}
    for line in out.splitlines():
        # 42  alsa_output.usb-FOO-00.analog-stereo  PipeWire  running  ...
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            mapping[parts[1]] = int(parts[0])
    return mapping

def set_sink_volume_by_name(sink_name: str, percent: int) -> bool:
    """Set volume of sink by its pactl name (or index in string form)."""
    percent = max(0, min(150, int(percent)))  # allow up to 150%
    rc, _, _ = _run(["pactl", "set-sink-volume", sink_name, f"{percent}%"])
    return rc == 0

def get_sink_volume_by_name(sink_name: str) -> int | None:
    if not sink_name:
        return None
    rc, out, _ = _run(["pactl", "list", "sinks"])
    if rc != 0:
        return None
    blocks = re.split(r"\n(?=Sink #\d+)", out)
    for block in blocks:
        if sink_name in block:
            match = re.search(r"Volume:.*?(\d+)%", block)
            if match:
                return int(match.group(1))
    return None

def set_device_volume(device_id: str, percent: int) -> bool:
    """
    device_id should match the pactl sink name your UI stores (e.g., `alsa_output.*`).
    Falls back to index lookup if needed.
    """
    if not device_id:
        return False
    # try as-is
    if set_sink_volume_by_name(device_id, percent):
        return True
    # try by index lookup
    sinks = list_sinks_short()
    if device_id in sinks:
        return set_sink_volume_by_name(str(sinks[device_id]), percent)
    # last resort: if device_id looks like an index
    if device_id.isdigit():
        return set_sink_volume_by_name(device_id, percent)
    return False

def get_device_volume(device_id: str) -> int | None:
    if not device_id:
        return None
    vol = get_sink_volume_by_name(device_id)
    if vol is not None:
        return vol
    sinks = list_sinks_short()
    if device_id in sinks:
        return get_sink_volume_by_name(str(sinks[device_id]))
    if device_id.isdigit():
        return get_sink_volume_by_name(device_id)
    return None

def set_default_volume(percent: int) -> bool:
    name = get_default_sink_name()
    return set_sink_volume_by_name(name, percent) if name else False


# =========================================================
# Shell helpers
# =========================================================

def _run_cmd(args: List[str], timeout: float = 3.0) -> tuple[int, str, str]:
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return cp.returncode, cp.stdout, cp.stderr
    except Exception as e:
        return 1, "", str(e)

def _pactl_json(kind: str) -> list:
    rc, out, _ = _run_cmd(["pactl", "-f", "json", "list", kind])
    if rc == 0 and out.strip():
        try:
            return json.loads(out)
        except Exception:
            return []
    return []

def _pactl_list(kind: str) -> str:
    rc, out, _ = _run_cmd(["pactl", "list", kind])
    return out if rc == 0 else ""


# =========================================================
# Sink-input inspection (JSON-first, text fallback)
# =========================================================
# --- add these near other helpers ---
def get_sink_input_indices_by_media(media_substr: str) -> list[int]:
    needle = (media_substr or "").lower()
    hits = []
    for si in _iter_sink_inputs_detailed():
        props = si.get("properties") or {}
        med = (props.get("media.name") or "").lower()
        if needle and needle in med and si.get("index") is not None:
            hits.append(int(si["index"]))
    return hits

def kill_sink_inputs_by_media(media_substr: str) -> int:
    """Force-kill any leftover sidecar instances by media name."""
    killed = 0
    for idx in get_sink_input_indices_by_media(media_substr):
        rc, _, _ = _run_cmd(["pactl", "kill-sink-input", str(idx)])
        if rc == 0:
            killed += 1
    return killed


def _sink_index_to_name() -> Dict[int, str]:
    """{index: name} for every sink — pactl's JSON reports a sink-input's
    target as an integer index, not a name."""
    out: Dict[int, str] = {}
    for name, idx in list_sinks_short().items():
        out[idx] = name
    return out


def _iter_sink_inputs_detailed() -> List[Dict]:
    js = _pactl_json("sink-inputs")
    out: List[Dict] = []
    if js:
        by_index = _sink_index_to_name()
        for item in js:
            raw_sink = item.get("sink")
            if isinstance(raw_sink, str):
                sink_name = raw_sink
            elif isinstance(raw_sink, int):
                sink_name = by_index.get(raw_sink)
            else:
                sink_name = None
            out.append({
                "index": item.get("index"),
                "sink": sink_name,
                "sink_name": sink_name,
                "driver": item.get("driver") or "",
                "properties": item.get("properties") or {}
            })
        return out

    txt = _pactl_list("sink-inputs")
    if not txt:
        return out

    blocks = re.split(r"\n(?=Sink Input #\d+)", txt)
    for b in blocks:
        m_idx = re.search(r"Sink Input #(\d+)", b)
        if not m_idx:
            continue
        idx = int(m_idx.group(1))
        props: Dict[str, str] = {}
        for key in ("application.name", "application.id", "media.name"):
            m = re.search(rf"{re.escape(key)} = \"([^\"]+)\"", b)
            if m:
                props[key] = m.group(1)
        sink_name = None
        m_sink = re.search(r"^Sink: (.+)$", b, flags=re.MULTILINE)
        if m_sink:
            sink_name = m_sink.group(1).strip()
        m_drv = re.search(r"^\s*Driver: (.+)$", b, flags=re.MULTILINE)
        out.append({
            "index": idx,
            "sink": sink_name,
            "sink_name": sink_name,
            "driver": m_drv.group(1).strip() if m_drv else "",
            "properties": props,
        })
    return out


# =========================================================
# AirPlay volume helpers
# =========================================================


def set_sink_input_linear(idx: int, linear: float):
    pct = max(0, min(150, int(linear * 100)))
    _run_cmd(["pactl", "set-sink-input-volume", str(idx), f"{pct}%"])

def _find_sink_inputs_by(app_substr: Optional[str] = None, media_substr: Optional[str] = None) -> list[dict]:
    hits = []
    for i in _iter_sink_inputs_detailed():
        props = i.get("properties") or {}
        app = (props.get("application.name") or "").lower()
        media = (props.get("media.name") or "").lower()
        ok_app = (app_substr is None) or (app_substr.lower() in app)
        ok_media = (media_substr is None) or (media_substr.lower() in media)
        if ok_app and ok_media:
            hits.append(i)
    return hits

def get_sink_input_indices_by_app(app_substr="rpiplay") -> list[int]:
    return [i.get("index") for i in _find_sink_inputs_by(app_substr=app_substr) if i.get("index") is not None]


class SinkWatcher:
    """Fires a callback whenever PulseAudio/PipeWire sinks appear or vanish.

    `pactl subscribe` is a long-lived process that prints one line per server
    event, so this costs a blocked read() on a daemon thread and nothing else —
    no polling timer, which matters on the Pi. Used to notice a Bluetooth sink
    going away while split audio is mirroring to it: PulseAudio then moves that
    leg's loopback onto the new default sink, so both legs land on the same
    device and the second one is still delay-compensated — the doubled,
    echoing playback the user hears as "two traces at the same time".
    """

    def __init__(self, on_sink_event: callable):
        self._cb = on_sink_event
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        if shutil.which("pactl") is None:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="SinkWatcher")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    ["pactl", "subscribe"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except Exception:
                return
            try:
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    low = line.lower()
                    # "Event 'remove' on sink #42" / 'new' / 'change'
                    if "on sink" in low or "on server" in low:
                        try:
                            self._cb()
                        except Exception:
                            pass
            except Exception:
                pass
            if self._stop.is_set():
                return
            # pactl died (server restart) — back off and re-attach.
            self._stop.wait(2.0)


# =========================================================
# AudioRouter — null bus + two loopbacks with per-sink latency
# =========================================================

DEFAULT_USER_DELAY_MS = 250  # runtime-tunable
BUS_SINK_NAME = "gtr_bus"

class AudioRouter:
    """
    - Mirror bus is created only when ensure_bus_mirror() is called (Apply Output).
    - Selected device is ALWAYS the second output; default sink remains main.
    - Latency: delay the faster leg by `user_delay_ms` when paired with BT, unless 'invert_delay' is True.
    - Quality: null bus at 48 kHz float, loopbacks use soxr; USB SPDIF → try analog profile/ports.
    - Live delay updates: set_user_delay()/set_delay_direction() rebuild mirror immediately if active.
    - Adaptive sync: BT loopback uses adjust_time=2 + auto-probes actual BT latency every 5 s.
    """
    _DEVICES_CACHE_TTL = 5.0  # seconds — sink topology rarely changes between toggles

    def __init__(self):
        self._bus_sink_name = BUS_SINK_NAME
        self._bus_modules: Dict[str, str] = {}   # {'null': id, 'lbk_def': id, 'lbk_sel': id}
        self._remembered: Tuple[Optional[str], Optional[str], int, int] = (None, None, 0, 0)  # (def, sel, d_def, d_sel)
        self.user_delay_ms = DEFAULT_USER_DELAY_MS
        self.invert_delay = False
        self._last_pair: Tuple[Optional[str], Optional[str]] = (None, None)
        self._probe_timer: Optional[threading.Timer] = None
        self._devices_cache: Optional[List[Dict]] = None
        self._devices_cache_ts: float = 0.0

    def invalidate_devices_cache(self) -> None:
        self._devices_cache = None
        self._devices_cache_ts = 0.0

    # ---------- Discovery ----------
    def list_devices(self) -> List[Dict]:
        now = time.monotonic()
        if (
            self._devices_cache is not None
            and (now - self._devices_cache_ts) < self._DEVICES_CACHE_TTL
        ):
            return list(self._devices_cache)
        by_name: Dict[str, Dict] = {}
        js = _pactl_json("sinks")
        if js:
            for item in js:
                sink_id = str(item.get("name") or item.get("properties", {}).get("node.name") or "").strip()
                if not sink_id:
                    continue
                props = item.get("properties") or {}
                desc = str(
                    props.get("device.description")
                    or props.get("node.description")
                    or props.get("device.nick")
                    or item.get("description")
                    or sink_id
                ).strip()
                by_name[sink_id] = {
                    "index": str(item.get("index", "")),
                    "id": sink_id,
                    "name": desc or sink_id,
                    "displayName": desc or sink_id,
                }

        rc, out, _ = _run_cmd(["pactl", "list", "short", "sinks"])
        if rc != 0 or not out:
            result = list(by_name.values())
            self._devices_cache = list(result)
            self._devices_cache_ts = now
            return result
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                sink_id = parts[1]
                existing = by_name.get(sink_id, {})
                display = existing.get("displayName") or sink_id
                by_name[sink_id] = {
                    "index": parts[0],
                    "id": sink_id,
                    "name": display,
                    "displayName": display,
                }
        result = list(by_name.values())
        self._devices_cache = list(result)
        self._devices_cache_ts = now
        return result

    def default_pulse_sink(self) -> Optional[str]:
        rc, out, _ = _run_cmd(["pactl", "info"])
        if rc != 0:
            return None
        for line in out.splitlines():
            if line.lower().startswith("default sink:"):
                return line.split(":", 1)[1].strip() or None
        return None

    # ---------- VLC hint ----------
    def apply_device_to_player(self, vlc_player, device: Optional[Dict]) -> None:
        if not device or not device.get("id"): return
        try:
            media = vlc_player.get_media()
            if media is not None:
                media.add_option(":aout=pulse")
        except Exception:
            pass

    # ---------- pactl helpers ----------
    def _load_module(self, name: str, args: List[str]) -> Optional[str]:
        rc, out, _ = _run_cmd(["pactl", "load-module", name] + args)
        if rc == 0:
            self.invalidate_devices_cache()
        return out.strip() if rc == 0 and out.strip() else None

    def _unload_module(self, mid: Optional[str]):
        if mid:
            _run_cmd(["pactl", "unload-module", mid])
            self.invalidate_devices_cache()

    def _sink_exists(self, sink_name: str) -> bool:
        rc, out, _ = _run_cmd(["pactl", "list", "short", "sinks"])
        if rc != 0: return False
        return any(("\t"+sink_name+"\t") in ("\t"+line+"\t") or (sink_name in line.split("\t")[1]) for line in out.splitlines())

    # ---------- Delay logic ----------
    def _compute_delays(self, default_sink: str, selected_sink: str) -> Tuple[int, int]:
        is_def_bt = "bluez_output." in (default_sink or "")
        is_sel_bt = "bluez_output." in (selected_sink or "")

        # If one side is BT and the other is not, normally delay the non-BT (faster).
        d_def, d_sel = 0, 0
        if is_def_bt and not is_sel_bt:
            # Default is BT (slow), Selected is fast
            d_def, d_sel = 0, self.user_delay_ms
        elif is_sel_bt and not is_def_bt:
            # Selected is BT (slow), Default is fast
            d_def, d_sel = self.user_delay_ms, 0

        if self.invert_delay:
            d_def, d_sel = d_sel, d_def
        return (d_def, d_sel)

    # ---------- BT latency probing ----------
    def probe_bt_latency_ms(self, sink_name: str) -> Optional[int]:
        """Read the BT sink's reported latency from pactl. Returns ms or None."""
        txt = _pactl_list("sinks")
        if not txt:
            return None
        blocks = re.split(r"\n(?=Sink #\d+)", txt)
        for b in blocks:
            if sink_name in b:
                m = re.search(r"Latency:\s*(\d+)\s*usec", b)
                if m:
                    return max(0, int(int(m.group(1)) / 1000))
        return None

    def sink_latency_ms(self, sink_name: str) -> Optional[int]:
        """Reported latency of any sink, Bluetooth or not."""
        return self.probe_bt_latency_ms(sink_name)

    def probe_pair_latency_ms(self) -> Tuple[Optional[int], str]:
        """Delay (ms) that lines the two legs up, plus a human-readable note.

        The old Auto button only worked when one leg was a `bluez_output.` sink
        and did nothing at all — silently — otherwise. Every sink reports a
        latency, so the honest number for *any* pair is the difference between
        the two, and the leg that needs delaying is whichever is faster. That
        also covers a USB DAC paired with the onboard jack.
        """
        def_sink, sel_sink = self._last_pair
        if not def_sink:
            return None, "Split is not active"
        if sel_sink is None:
            return None, "Single-device 4-channel mode needs no offset"

        l_def = self.sink_latency_ms(def_sink)
        l_sel = self.sink_latency_ms(sel_sink)
        if l_def is None and l_sel is None:
            return None, "Neither output reports a latency"
        if l_def is None or l_sel is None:
            # One side is silent about it; fall back to the side we do know,
            # which is what the Bluetooth-only path used to assume.
            known = l_def if l_def is not None else l_sel
            return max(0, min(2000, int(known))), "Measured one output only"

        diff = int(abs(l_def - l_sel))
        if diff == 0:
            # Both read zero when the sinks are suspended, which is not the
            # same as "they are already aligned" — say so rather than claiming
            # a measurement.
            if not l_def and not l_sel:
                return 0, "Both outputs idle — play something, then measure"
            return 0, "Both outputs report the same latency"
        slower = "main" if l_def > l_sel else "second"
        return max(0, min(2000, diff)), f"{diff} ms — {slower} output is slower"

    def _cancel_probe(self) -> None:
        if self._probe_timer:
            self._probe_timer.cancel()
            self._probe_timer = None

    # ---------- Channel split (L→front, R→back) ----------
    def _make_lbk_args(self, source: str, sink: str, is_bt: bool,
                       latency_ms: int, channel_map: Optional[str] = None) -> List[str]:
        args = [
            f"source={source}",
            f"sink={sink}",
            f"latency_msec={latency_ms}",
            "resample_method=soxr",
        ]
        if channel_map:
            args += [f"channel_map={channel_map}", "remix=no"]
        if is_bt:
            args += ["adjust_time=2", "max_latency_msec=500", "fast_adjust_threshold_msec=40"]
        else:
            args += ["adjust_time=4"]
        return args

    def set_channel_split(self, split: bool, invert_lr: bool = False) -> None:
        """
        Two-device mode only (sel_sink is not None).
        split=True  → extract mono L (or R if inverted) via module-remap-source, route to each device.
        split=False → both devices get full stereo loopbacks from gtr_bus.monitor.
        The null sink stays running — no gap in routing.
        """
        def_sink, sel_sink = self._last_pair
        if not def_sink:
            return
        if sel_sink is None:
            # Single-device 4-channel mode. This used to return here, so the
            # F|B button was a no-op on a surround card — pressing it changed
            # nothing, which is exactly "front and back sound the same".
            self._set_channel_split_4ch(split, invert_lr)
            return
        if "null" not in self._bus_modules:
            return

        # Tear down existing remap-sources and loopbacks
        for key in ("src_L", "src_R", "lbk_sel", "lbk_def"):
            self._unload_module(self._bus_modules.pop(key, None))

        src = f"{self._bus_sink_name}.monitor"
        is_def_bt = "bluez_output." in def_sink
        is_sel_bt = "bluez_output." in sel_sink
        d_def, d_sel = self._compute_delays(def_sink, sel_sink)

        if split:
            # Use remap-source to cleanly extract a single channel as mono.
            # invert_lr swaps which channel goes to front vs. back.
            l_ch = "front-right" if invert_lr else "front-left"
            r_ch = "front-left" if invert_lr else "front-right"

            mid_src_l = self._load_module("module-remap-source", [
                "source_name=gtr_L_mono",
                f"master={src}",
                "channels=1",
                f"channel_map={l_ch}",
                "master_channel_map=front-left,front-right",
                "remix=no",
            ])
            if mid_src_l:
                self._bus_modules["src_L"] = mid_src_l

            mid_src_r = self._load_module("module-remap-source", [
                "source_name=gtr_R_mono",
                f"master={src}",
                "channels=1",
                f"channel_map={r_ch}",
                "master_channel_map=front-left,front-right",
                "remix=no",
            ])
            if mid_src_r:
                self._bus_modules["src_R"] = mid_src_r

            l_name = "gtr_L_mono" if mid_src_l else src
            r_name = "gtr_R_mono" if mid_src_r else src

            mid_def = self._load_module("module-loopback",
                                        self._make_lbk_args(l_name, def_sink, is_def_bt, d_def))
            if mid_def:
                self._bus_modules["lbk_def"] = mid_def

            mid_sel = self._load_module("module-loopback",
                                        self._make_lbk_args(r_name, sel_sink, is_sel_bt, d_sel))
            if mid_sel:
                self._bus_modules["lbk_sel"] = mid_sel
        else:
            mid_def = self._load_module("module-loopback",
                                        self._make_lbk_args(src, def_sink, is_def_bt, d_def))
            if mid_def:
                self._bus_modules["lbk_def"] = mid_def

            mid_sel = self._load_module("module-loopback",
                                        self._make_lbk_args(src, sel_sink, is_sel_bt, d_sel))
            if mid_sel:
                self._bus_modules["lbk_sel"] = mid_sel

    def _set_channel_split_4ch(self, split: bool, invert_lr: bool = False) -> None:
        """Rebuild the 4-channel loopback either mirrored or hard-split.

        Mirrored (split=False) is the default: FL=L FR=R RL=L RR=R, both pairs
        carrying the same stereo image. Split=True sends L to the front pair and
        R to the rear pair, so the two ends of the car genuinely differ.
        """
        def_sink, sel_sink = self._last_pair
        if not def_sink or sel_sink is not None:
            return
        if "lbk_4ch" not in self._bus_modules:
            return
        is_bt = "bluez_output." in def_sink
        src = f"{self._bus_sink_name}.monitor"
        self._unload_module(self._bus_modules.pop("lbk_4ch", None))
        if split:
            # Hard split: one source channel to both front speakers, the other
            # to both rears, so the two ends of the car carry different audio.
            chan4 = ("front-right,front-right,front-left,front-left" if invert_lr
                     else "front-left,front-left,front-right,front-right")
        else:
            chan4 = ("front-right,front-left,rear-right,rear-left" if invert_lr
                     else "front-left,front-right,rear-left,rear-right")
        mid = self._load_module("module-loopback", self._make_lbk_args(src, def_sink, is_bt, 0, chan4))
        if mid:
            self._bus_modules["lbk_4ch"] = mid

    # ---------- Front/rear balance ----------
    def set_front_rear_balance(self, balance: int) -> bool:
        """Bias level between the two legs. -100 = front only, +100 = rear only.

        A hard L/R channel split is barely audible as "front vs back" on
        ordinary stereo music, because L and R carry almost the same content.
        Level is what actually makes the two ends of the car distinguishable,
        so the fader is applied to the loopback streams themselves rather than
        to the sinks — the sinks keep whatever volume the user set for them.
        """
        b = max(-100, min(100, int(balance)))
        front_gain = 1.0 if b <= 0 else (1.0 - b / 100.0)
        rear_gain  = 1.0 if b >= 0 else (1.0 + b / 100.0)

        def_sink, sel_sink = self._last_pair
        if not def_sink:
            return False
        if sel_sink is None:
            # 4ch mode: both pairs ride one loopback, so there is nothing to
            # bias between. Report failure so the UI can hide the control.
            return False

        ok = False
        for key, gain in (("lbk_def", front_gain), ("lbk_sel", rear_gain)):
            if key not in self._bus_modules:
                continue
            sink = def_sink if key == "lbk_def" else sel_sink
            for idx in self._loopback_input_indices(sink):
                set_sink_input_linear(idx, gain)
                ok = True
        return ok

    def _loopback_input_indices(self, target_sink: str) -> List[int]:
        """Sink-input indices of our own loopbacks feeding `target_sink`.

        Identified by target sink plus a mention of the bus we created — the
        `driver` field is "PipeWire" for everything under pipewire-pulse, so it
        cannot be used to spot a loopback on the Pi.
        """
        out: List[int] = []
        needles = (self._bus_sink_name.lower(), "laladash_mirrorbus", "loopback")
        for i in _iter_sink_inputs_detailed():
            sink_name = i.get("sink_name") or i.get("sink")
            if not sink_name or sink_name != target_sink:
                continue
            props = i.get("properties") or {}
            haystack = " ".join(
                str(props.get(k) or "")
                for k in ("media.name", "node.name", "application.name",
                          "device.description", "node.description")
            ).lower()
            haystack += " " + str(i.get("driver") or "").lower()
            if not any(n in haystack for n in needles):
                continue
            idx = i.get("index")
            if idx is not None:
                out.append(idx)
        return out

    def set_invert_lr(self, invert: bool) -> None:
        """In 4ch single-device mode, rebuild the primary loopback with L/R swapped."""
        def_sink, sel_sink = self._last_pair
        if not def_sink or sel_sink is not None:
            return  # not in 4ch mode
        if "lbk_4ch" not in self._bus_modules:
            return
        is_bt = "bluez_output." in def_sink
        src = f"{self._bus_sink_name}.monitor"
        self._unload_module(self._bus_modules.pop("lbk_4ch", None))
        chan4 = ("front-right,front-left,rear-right,rear-left" if invert
                 else "front-left,front-right,rear-left,rear-right")
        mid = self._load_module("module-loopback", self._make_lbk_args(src, def_sink, is_bt, 0, chan4))
        if mid:
            self._bus_modules["lbk_4ch"] = mid

    # ---------- USB SPDIF → analog attempts ----------
    def _get_card_for_sink(self, sink_name: str) -> Optional[str]:
        js = _pactl_json("sinks")
        if js:
            for s in js:
                if (s.get("name") == sink_name) or (s.get("properties", {}).get("node.name") == sink_name):
                    return s.get("card")
        txt = _pactl_list("sinks")
        blocks = re.split(r"\n(?=Sink #\d+)", txt)
        for b in blocks:
            if f"Name: {sink_name}" in b:
                m = re.search(r"Card: (.+)", b)
                if m:
                    return m.group(1).strip()
        return None

    def _pactl_card_profiles(self, card_name: str) -> List[Dict]:
        """Return the profiles list for a named card from pactl JSON."""
        cards = _pactl_json("cards")
        for card in cards:
            if card.get("name") == card_name or str(card.get("index", "")) == str(card_name):
                profiles = card.get("profiles") or {}
                if isinstance(profiles, dict):
                    return [{"name": k, **v} for k, v in profiles.items()]
                if isinstance(profiles, list):
                    return profiles
        return []

    def card_supports_surround40(self, sink_name: str) -> bool:
        """Return True if the sink's card has an available output:analog-surround-40 profile."""
        if not sink_name:
            return False
        card_name = self._get_card_for_sink(sink_name)
        if not card_name:
            return False
        for p in self._pactl_card_profiles(card_name):
            name = p.get("name", "")
            available = str(p.get("available", "")).lower()
            if "analog-surround-40" in name and available not in ("no", "false", "0", ""):
                return True
        return False

    def force_surround40_profile(self, sink_name: str) -> bool:
        card = self._get_card_for_sink(sink_name)
        if not card:
            return False
        return self._try_set_card_profile(card, ["output:analog-surround-40"])

    def _try_set_sink_port(self, sink_name: str, ports: List[str]) -> bool:
        for p in ports:
            rc, _, _ = _run_cmd(["pactl", "set-sink-port", sink_name, p])
            if rc == 0:
                return True
        return False

    def _try_set_card_profile(self, card: str, profiles: List[str]) -> bool:
        for prof in profiles:
            rc, _, _ = _run_cmd(["pactl", "set-card-profile", card, prof])
            if rc == 0:
                return True
        return False

    def optimize_sink_quality(self, sink_name: str) -> None:
        low = (sink_name or "").lower()
        looks_spdif = ("iec958" in low) or ("spdif" in low) or ("digital" in low)

        # Try common analog ports first
        self._try_set_sink_port(sink_name, [
            "analog-output",
            "analog-output-speaker",
            "analog-output-lineout",
        ])

        # If still likely SPDIF/digital, switch card profile to analog if possible
        if looks_spdif:
            card = self._get_card_for_sink(sink_name)
            if card:
                self._try_set_card_profile(card, [
                    "output:analog-stereo",
                    "pro-audio"
                ])
                self._try_set_sink_port(sink_name, [
                    "analog-output",
                    "analog-output-speaker",
                    "analog-output-lineout",
                ])

    # ---------- 4-channel single-device mode ----------
    def _ensure_4ch_mode(self, sink: str) -> bool:
        """Set up the routing bus for a single surround-40 USB card.

        gtr_bus (4ch null sink)
         ├── VLC writes stereo → FL, FR filled
         ├── src_rear_map (remap-source: relabels gtr_bus.monitor FL,FR → RL,RR)
         │    └── lbk_rear_mirror (loopback → gtr_bus: fills RL, RR)
         └── lbk_4ch (loopback → physical sink, 4 channels end-to-end)
        Result on card: FL=L  FR=R  RL=L  RR=R
        """
        src = f"{self._bus_sink_name}.monitor"
        chan4 = "front-left,front-right,rear-left,rear-right"

        if self._remembered != (sink, None, 0, 0):
            self.teardown_bus()

        # 4-channel null sink
        if not self._sink_exists(self._bus_sink_name):
            mid_null = self._load_module("module-null-sink", [
                f"sink_name={self._bus_sink_name}",
                "format=float32le", "rate=48000", "channels=4",
                f"channel_map={chan4}",
                "sink_properties=device.description=LaLaDash_MirrorBus_4ch",
            ])
            if not mid_null:
                return False
            self._bus_modules["null"] = mid_null
            time.sleep(0.05)

        # Remap-source: reads FL,FR from gtr_bus.monitor, labels them RL,RR
        if "src_rear_map" not in self._bus_modules:
            mid_src = self._load_module("module-remap-source", [
                f"source_name={self._bus_sink_name}_as_rear",
                f"master={src}",
                "channels=2",
                "channel_map=rear-left,rear-right",
                "master_channel_map=front-left,front-right",
                "remix=no",
            ])
            if not mid_src:
                self.teardown_bus(); return False
            self._bus_modules["src_rear_map"] = mid_src

        # Loopback from that remap-source → gtr_bus (fills the RL,RR channels)
        if "lbk_rear_mirror" not in self._bus_modules:
            mid_rm = self._load_module("module-loopback", [
                f"source={self._bus_sink_name}_as_rear",
                f"sink={self._bus_sink_name}",
                "latency_msec=0",
                "resample_method=soxr",
                "adjust_time=4",
                "remix=no",
            ])
            if not mid_rm:
                self.teardown_bus(); return False
            self._bus_modules["lbk_rear_mirror"] = mid_rm

        # Primary 4-channel loopback → physical card
        if "lbk_4ch" not in self._bus_modules:
            is_bt = "bluez_output." in sink
            mid = self._load_module("module-loopback",
                                    self._make_lbk_args(src, sink, is_bt, 0, chan4))
            if not mid:
                self.teardown_bus(); return False
            self._bus_modules["lbk_4ch"] = mid

        self._remembered = (sink, None, 0, 0)
        self._last_pair = (sink, None)
        return True

    # ---------- Engine-sound rear remap sink ----------
    def ensure_rear_remap_sink(self) -> Optional[str]:
        """Create a virtual sink that accepts stereo and writes it to gtr_bus's rear channels.
        Only valid when 4ch mode is active. Returns the sink name or None on failure."""
        def_sink, sel_sink = self._last_pair
        if not def_sink or sel_sink is not None:
            return None  # not in 4ch mode
        if not self._sink_exists(self._bus_sink_name):
            return None
        if "engine_rear" not in self._bus_modules:
            mid = self._load_module("module-remap-sink", [
                "sink_name=gtr_engine_rear",
                f"master={self._bus_sink_name}",
                "channels=2",
                "channel_map=front-left,front-right",
                "master_channel_map=rear-left,rear-right",
                "remix=no",
            ])
            if not mid:
                return None
            self._bus_modules["engine_rear"] = mid
        return "gtr_engine_rear"

    def teardown_rear_remap_sink(self) -> None:
        mid = self._bus_modules.pop("engine_rear", None)
        self._unload_module(mid)

    # ---------- Mirror bus (two-device) ----------
    def ensure_bus_mirror(self, target_sink: str) -> bool:
        default_sink = self.default_pulse_sink()
        if not default_sink:
            return False

        # Auto-detect: if the default output supports surround-40, use single-device 4ch mode.
        if self.card_supports_surround40(default_sink):
            if not self.force_surround40_profile(default_sink):
                return False
            return self._ensure_4ch_mode(default_sink)

        # Two-device mode requires a distinct second device.
        if not target_sink or target_sink == default_sink:
            return False

        # Improve selected sink quality if it came up as digital-only
        self.optimize_sink_quality(target_sink)

        is_def_bt = "bluez_output." in default_sink
        is_sel_bt = "bluez_output." in target_sink

        d_def, d_sel = self._compute_delays(default_sink, target_sink)

        # Rebuild if pair or delays changed
        if self._remembered != (default_sink, target_sink, d_def, d_sel):
            self.teardown_bus()

        src = f"{self._bus_sink_name}.monitor"

        # Create null bus (48k float, stereo)
        if not self._sink_exists(self._bus_sink_name):
            mid_null = self._load_module("module-null-sink", [
                f"sink_name={self._bus_sink_name}",
                "format=float32le", "rate=48000", "channels=2",
                "sink_properties=device.description=LaLaDash_MirrorBus"
            ])
            if not mid_null:
                return False
            self._bus_modules["null"] = mid_null
            time.sleep(0.05)

        # Loopback to Default (main)
        if "lbk_def" not in self._bus_modules:
            mid_def = self._load_module("module-loopback",
                                        self._make_lbk_args(src, default_sink, is_def_bt, d_def))
            if not mid_def:
                self.teardown_bus(); return False
            self._bus_modules["lbk_def"] = mid_def

        # Loopback to Selected (second)
        if "lbk_sel" not in self._bus_modules:
            mid_sel = self._load_module("module-loopback",
                                        self._make_lbk_args(src, target_sink, is_sel_bt, d_sel))
            if not mid_sel:
                self.teardown_bus(); return False
            self._bus_modules["lbk_sel"] = mid_sel

        self._remembered = (default_sink, target_sink, d_def, d_sel)
        self._last_pair = (default_sink, target_sink)
        return True

    def teardown_bus(self) -> None:
        self._cancel_probe()
        # Unload in dependency order: engine overlay first, then routing, then bus
        for key in ("engine_rear", "lbk_rear_mirror", "src_rear_map",
                    "lbk_4ch", "src_L", "src_R", "lbk_sel", "lbk_def", "null"):
            mid = self._bus_modules.pop(key, None)
            self._unload_module(mid)
        self._remembered = (None, None, 0, 0)
        self._last_pair = (None, None)

    # ---------- Live delay / direction change ----------
    def set_user_delay(self, ms: int) -> None:
        """Update user delay and rebuild mirror immediately if active."""
        self.user_delay_ms = max(0, min(2000, int(ms)))
        def_sink, sel_sink = self._last_pair
        if def_sink and sel_sink:
            if self.ensure_bus_mirror(sel_sink):
                self.move_main_to_bus("GTR_MAIN")

    def set_delay_direction(self, invert: bool) -> None:
        """If True, swap which leg is delayed. Rebuild immediately if active."""
        self.invert_delay = bool(invert)
        def_sink, sel_sink = self._last_pair
        if def_sink and sel_sink:
            if self.ensure_bus_mirror(sel_sink):
                self.move_main_to_bus("GTR_MAIN")

    # ---------- Moving streams ----------
    def _move_indices_to_sink(self, indices: List[int], target_sink: str) -> int:
        moved = 0
        for idx in indices:
            rc, _, _ = _run_cmd(["pactl", "move-sink-input", str(idx), target_sink])
            if rc == 0:
                moved += 1
        return moved

    def move_inputs_by_media_name(self, media_name: str, target_sink: str) -> int:
        if not (media_name and target_sink):
            return 0
        needle = media_name.lower()
        indices: List[int] = []
        for si in _iter_sink_inputs_detailed():
            props = si.get("properties") or {}
            app = (props.get("application.name") or "").lower()
            mid = (props.get("application.id") or "").lower()
            med = (props.get("media.name") or "").lower()
            if needle in med or needle in app or needle in mid:
                if si.get("index") is not None:
                    indices.append(int(si["index"]))
        return self._move_indices_to_sink(indices, target_sink)

    def move_vlc_inputs(self, target_sink: str) -> int:
        if not target_sink: return 0
        indices: List[int] = []
        for si in _iter_sink_inputs_detailed():
            props = si.get("properties") or {}
            app = (props.get("application.name") or "").lower()
            mid = (props.get("application.id") or "").lower()
            if "vlc media player" in app or "org.videolan.vlc" in mid:
                if si.get("index") is not None:
                    indices.append(int(si["index"]))
        return self._move_indices_to_sink(indices, target_sink)

    def move_main_to_bus(self, media_name: str = "GTR_MAIN") -> bool:
        if not self._sink_exists(self._bus_sink_name):
            return False
        moved = self.move_inputs_by_media_name(media_name, self._bus_sink_name)
        if moved == 0:
            moved = self.move_vlc_inputs(self._bus_sink_name)
        return moved > 0

    def move_main_to_sink(self, target_sink: str, media_name: str = "GTR_MAIN") -> bool:
        if not target_sink:
            return False
        moved = self.move_inputs_by_media_name(media_name, target_sink)
        if moved == 0:
            moved = self.move_vlc_inputs(target_sink)
        return moved > 0

    # ---------- Bass sidecar ----------
    def move_bass_sidecar(self, target_sink: str) -> int:
        return self.move_inputs_by_media_name("GTR_BASS_SIDECAR", target_sink)

    # ---------- Cleanup ----------
    def cleanup(self) -> None:
        self.teardown_bus()


# =========================================================
# Global helpers — bind these to side menu buttons and startup/exit
# =========================================================

def force_cleanup_any_laladash_modules():
    """
    Best-effort cleanup in case the app crashed and left modules around.
    Safe to call at startup and on exit.
    """
    rc, out, _ = _run_cmd(["pactl", "list", "short", "modules"])
    if rc != 0 or not out:
        return
    to_unload = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        mid, mname = parts[0], parts[1]
        args = parts[2] if len(parts) > 2 else ""
        if (
            ("module-loopback" in mname and (
                "source=gtr_bus.monitor" in args
                or "source=gtr_L_mono" in args
                or "source=gtr_R_mono" in args
                or "source=gtr_bus_as_rear" in args
            ))
            or ("module-null-sink" in mname and "sink_name=gtr_bus" in args)
            or ("module-combine-sink" in mname and "sink_name=gtr_mirror" in args)
            or ("module-remap-source" in mname and (
                "source_name=gtr_L_mono" in args
                or "source_name=gtr_R_mono" in args
                or "source_name=gtr_bus_as_rear" in args
            ))
            or ("module-remap-sink" in mname and "sink_name=gtr_engine_rear" in args)
        ):
            to_unload.append(mid)
    for mid in to_unload:
        _run_cmd(["pactl", "unload-module", mid])

def _get_default_sink() -> Optional[str]:
    rc, out, _ = _run_cmd(["pactl", "info"])
    if rc != 0: return None
    for line in out.splitlines():
        if line.lower().startswith("default sink:"):
            return line.split(":", 1)[1].strip() or None
    return None

def _get_active_mirror_sinks() -> List[str]:
    rc, out, _ = _run_cmd(["pactl", "list", "short", "modules"])
    if rc != 0 or not out: return []
    sinks: List[str] = []
    for line in out.splitlines():
        if "module-loopback" in line and "source=gtr_bus.monitor" in line and "sink=" in line:
            parts = line.split("\t")
            if len(parts) >= 3:
                args = parts[2]
                m = re.search(r"sink=([^\s]+)", args)
                if m:
                    sinks.append(m.group(1))
    return sinks

def change_system_volume(delta_pct: int):
    """
    Volume step for side-menu buttons. If Mirror is active, apply to both sinks (Default+Selected).
    Otherwise, apply to @DEFAULT_SINK@ only.
    """
    sinks = _get_active_mirror_sinks()
    if not sinks:
        sink = _get_default_sink() or "@DEFAULT_SINK@"
        _run_cmd(["pactl", "set-sink-volume", sink, f"{'+' if delta_pct>0 else ''}{delta_pct}%"])
        return
    for s in sinks:
        _run_cmd(["pactl", "set-sink-volume", s, f"{'+' if delta_pct>0 else ''}{delta_pct}%"])

def set_system_volume(percent: int):
    """
    Absolute volume setter (0..150). Same multi-sink behavior as change_system_volume.
    """
    pct = max(0, min(150, int(percent)))
    sinks = _get_active_mirror_sinks()
    if not sinks:
        sink = _get_default_sink() or "@DEFAULT_SINK@"
        _run_cmd(["pactl", "set-sink-volume", sink, f"{pct}%"])
        return
    for s in sinks:
        _run_cmd(["pactl", "set-sink-volume", s, f"{pct}%"])
