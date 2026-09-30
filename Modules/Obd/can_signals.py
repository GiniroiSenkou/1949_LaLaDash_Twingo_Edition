"""CAN signal map: which frame ID / bytes / bits mean what on this car.

The bus carries ~45 IDs and Renault publishes no dictionary, so every
signal here was (or will be) found by toggling the real thing while
recording — see Data/can_captures/README.md. The debugger sheet writes this
map; the runtime reads it: a `confirmed` signal is what feeds the app, a
`candidate` shows live in the debugger's Assign tab until the user confirms
or clears it, `unassigned` is a catalog entry with no binding yet.

Data/can_signal_map.json (hand-editable):

    {"version": 1, "vehicle": "…", "bus": "ATSP6 500k",
     "signals": {
       "low_beam":  {"id": "0x5DE", "type": "bool", "byte": 0, "bit": 1, "status": "confirmed"},
       "rpm":       {"id": "0x186", "type": "number", "start_byte": 0, "length": 2,
                     "endian": "big", "scale": 0.125, "offset": 0, "signed": false, "status": "candidate"},
       "gear":      {"id": "0x418", "type": "enum", "start_byte": 0, "length": 1,
                     "values": {"0x4E": "N", "0x31": "1"}, "status": "unassigned"}},
     "ids": {"0x5DE": {"label": "BCM exterior lights"}}}

Targets (what a signal drives once confirmed):
  metric:<name>  → TelemetryHub.ingest_can(): the metric stops polling its PID
  light:<key>    → DashLightsHub active set (the camera detector's own entry)
  clutch         → TelemetryHub.update_clutch_from_can() (AudioEngine shift detection)
  state:<key>    → snapshot entry "CAN <key>" (ignition, engine_on, reverse, brake…)
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from preferences import BASE_DIR

MAP_PATH = os.path.join(BASE_DIR, "Data", "can_signal_map.json")
STATUSES = ("confirmed", "candidate", "unassigned")

# key, label, group, kind, target
SIGNAL_CATALOG: List[Tuple[str, str, str, str, str]] = [
    # ── telemetry metrics (replace PID polling when confirmed) ──
    ("rpm",          "Engine RPM",             "Engine",        "number", "metric:RPM"),
    ("speed",        "Vehicle speed",          "Driving",       "number", "metric:Speed"),
    ("throttle",     "Throttle position",      "Engine",        "number", "metric:Throttle Pos"),
    ("accel_pedal",  "Accelerator pedal",      "Pedals",        "number", "metric:Accel Pedal D"),
    ("coolant",      "Coolant temperature",    "Engine",        "number", "metric:Coolant"),
    ("oil_temp",     "Oil temperature",        "Engine",        "number", "metric:Oil Temp"),
    ("intake_air",   "Intake air temperature", "Engine",        "number", "metric:Intake Air"),
    ("manifold",     "Manifold pressure",      "Engine",        "number", "metric:Manifold Press"),
    ("fuel_level",   "Fuel level",             "Fuel",          "number", "metric:Fuel Level"),
    ("ecu_voltage",  "Battery voltage",        "Electrical",    "number", "metric:ECU Voltage"),
    ("battery_soc",  "Battery charge %",       "Electrical",    "number", "metric:Battery SoC"),
    # The reference car's Stop&Start battery sensor (on the negative terminal) is
    # what really knows the battery; if its frames are on the bus these two
    # make the voltage estimate and its learned full point redundant.
    ("battery_current", "Battery current (A, +charge)", "Electrical", "number", "metric:Battery Current"),
    ("battery_soh",  "Battery health %",       "Electrical",    "number", "metric:Battery SoH"),
    ("steering",     "Steering angle",         "Driving",       "number", "metric:Steering"),
    ("timing_adv",   "Timing advance",         "Engine",        "number", "metric:Timing Adv"),
    ("equiv_ratio",  "Lambda / equiv. ratio",  "Engine",        "number", "metric:Equiv. Ratio"),
    ("fuel_trim_s",  "Short-term fuel trim",   "Fuel",          "number", "metric:Fuel Trim S"),
    ("fuel_trim_l",  "Long-term fuel trim",    "Fuel",          "number", "metric:Fuel Trim L"),
    ("abs_load",     "Absolute load",          "Engine",        "number", "metric:Absolute Load"),
    ("fuel_pressure", "Fuel pressure",         "Fuel",          "number", "metric:Fuel Pressure"),
    ("baro",         "Barometric pressure",    "Engine",        "number", "metric:Baro Press"),
    ("gear",         "Gear",                   "Driving",       "enum",   "metric:Gear"),
    # ── pedals / states ──
    ("clutch",       "Clutch pedal",           "Pedals",        "bool",   "clutch"),
    ("brake",        "Brake pedal",            "Pedals",        "bool",   "state:brake"),
    ("ignition",     "Ignition on",            "States",        "bool",   "state:ignition"),
    ("engine_on",    "Engine running",         "States",        "bool",   "state:engine_on"),
    ("reverse",      "Reverse gear",           "States",        "bool",   "state:reverse"),
    ("handbrake",    "Handbrake",              "States",        "bool",   "light:parking"),
    # ── exterior lights (dash icons + info alerts) ──
    ("low_beam",     "Low beam",               "Lights",        "bool",   "light:low_beam"),
    ("high_beam",    "High beam",              "Lights",        "bool",   "light:high_beam"),
    ("position_lights", "Position / DRL",      "Lights",        "bool",   "light:position_lights"),
    ("fog_front",    "Front fog lights",       "Lights",        "bool",   "light:fog_lights_front"),
    ("fog_rear",     "Rear fog light",         "Lights",        "bool",   "light:fog_lights_rear"),
    ("turn_left",    "Left indicator",         "Lights",        "bool",   "light:left_turn"),
    ("turn_right",   "Right indicator",        "Lights",        "bool",   "light:right_turn"),
    ("hazard",       "Hazard switch",          "Lights",        "bool",   "state:hazard"),
    # ── warning lamps (dash icons + warning/critical alerts) ──
    ("mil",          "Check engine (MIL)",     "Warning lamps", "bool",   "light:engine"),
    ("oil",          "Oil pressure",           "Warning lamps", "bool",   "light:oil"),
    ("abs",          "ABS fault",              "Warning lamps", "bool",   "light:abs"),
    ("battery",      "Battery / charging",     "Warning lamps", "bool",   "light:battery"),
    ("coolant_temp", "Coolant temperature",    "Warning lamps", "bool",   "light:coolant_temperature"),
    ("petrol",       "Low fuel",               "Warning lamps", "bool",   "light:petrol"),
    ("doors_open",   "Door open",              "Warning lamps", "bool",   "light:doors_open"),
    ("seatbelt",     "Seatbelt",               "Warning lamps", "bool",   "light:seatbelt"),
    ("tyres",        "Tyre pressure",          "Warning lamps", "bool",   "light:tyres"),
    ("airbag",       "Airbag fault",           "Warning lamps", "bool",   "light:airbag"),
    ("stop",         "STOP",                   "Warning lamps", "bool",   "light:stop"),
    ("steering_fault", "Steering fault",       "Warning lamps", "bool",   "light:steering_fault"),
    ("efl",          "EFL",                    "Warning lamps", "bool",   "light:efl"),
    ("speed_limiter", "Speed limiter",         "Warning lamps", "bool",   "light:speed_limiter"),
    ("eco",          "Eco mode",               "Warning lamps", "bool",   "light:eco"),
]
CATALOG_BY_KEY = {c[0]: c for c in SIGNAL_CATALOG}


def _parse_id(v) -> int:
    if isinstance(v, int):
        return v
    s = str(v).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s, 16)


def _fmt_id(ident: int) -> str:
    return f"0x{int(ident):03X}"


# ── decoding ──────────────────────────────────────────────────────────────
def decode_one(spec: dict, data: bytes):
    """Value of one signal from one payload, or None when it does not fit."""
    kind = spec.get("type", "bool")
    try:
        if kind == "bool":
            b = int(spec.get("byte", 0))
            bit = int(spec.get("bit", 0))
            if b >= len(data):
                return None
            v = bool((data[b] >> bit) & 1)
            return (not v) if spec.get("invert") else v
        start = int(spec.get("start_byte", 0))
        length = max(1, min(4, int(spec.get("length", 1))))
        if start + length > len(data):
            return None
        chunk = data[start:start + length]
        raw = int.from_bytes(chunk, "little" if spec.get("endian") == "little" else "big",
                             signed=bool(spec.get("signed")))
        mask = spec.get("mask")
        if mask is not None:
            raw &= int(str(mask), 0)
        shift = int(spec.get("shift", 0) or 0)
        if shift:
            raw >>= shift
        if kind == "enum":
            values = spec.get("values") or {}
            for k, label in values.items():
                try:
                    if int(str(k), 0) == raw:
                        return label
                except ValueError:
                    continue
            return None
        return raw * float(spec.get("scale", 1.0)) + float(spec.get("offset", 0.0))
    except Exception:
        return None


class CanSignalMap:
    """Load/save the map; decode a FrameStore into signal values."""

    def __init__(self, path: str = MAP_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.vehicle = ""
        self.bus = ""
        self.signals: Dict[str, dict] = {}
        self.ids: Dict[str, dict] = {}
        self.load()

    # ── persistence ──
    def load(self) -> None:
        with self._lock:
            data: dict = {}
            try:
                with open(self.path) as f:
                    data = json.load(f)
            except FileNotFoundError:
                data = {}
            except Exception as exc:
                print(f"[can] signal map unreadable ({exc}); starting empty", flush=True)
                data = {}
            self.vehicle = str(data.get("vehicle", ""))
            self.bus = str(data.get("bus", ""))
            self.signals = {}
            for key, spec in (data.get("signals") or {}).items():
                if isinstance(spec, dict) and key in CATALOG_BY_KEY:
                    spec = dict(spec)
                    if spec.get("status") not in STATUSES:
                        spec["status"] = "candidate" if spec.get("id") else "unassigned"
                    self.signals[key] = spec
            self.ids = {str(k): dict(v) for k, v in (data.get("ids") or {}).items() if isinstance(v, dict)}

    def save(self) -> bool:
        with self._lock:
            payload = {
                "version": 1,
                "vehicle": self.vehicle,
                "bus": self.bus,
                "signals": {k: self.signals[k] for k in sorted(self.signals)},
                "ids": {k: self.ids[k] for k in sorted(self.ids)},
            }
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(tmp, "w") as f:
                    json.dump(payload, f, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return True
            except Exception as exc:
                print(f"[can] signal map save failed: {exc}", flush=True)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return False

    # ── editing ──
    def assign(self, key: str, spec: dict, status: str = "candidate") -> bool:
        if key not in CATALOG_BY_KEY or "id" not in spec:
            return False
        with self._lock:
            clean = dict(spec)
            clean["id"] = _fmt_id(_parse_id(clean["id"]))
            clean.setdefault("type", CATALOG_BY_KEY[key][3])
            clean["status"] = status if status in STATUSES else "candidate"
            self.signals[key] = clean
            return self.save()

    def set_status(self, key: str, status: str) -> bool:
        with self._lock:
            if key not in self.signals or status not in STATUSES:
                return False
            self.signals[key]["status"] = status
            return self.save()

    def clear(self, key: str) -> bool:
        with self._lock:
            if key in self.signals:
                del self.signals[key]
                return self.save()
            return True

    def label_id(self, ident: int, label: str) -> bool:
        with self._lock:
            self.ids.setdefault(_fmt_id(ident), {})["label"] = label
            return self.save()

    # ── queries ──
    def id_status(self, ident: int) -> str:
        """confirmed > candidate > unknown, by the best signal on that ID."""
        key = _fmt_id(ident)
        best = "unknown"
        with self._lock:
            for spec in self.signals.values():
                if spec.get("id") == key:
                    st = spec.get("status")
                    if st == "confirmed":
                        return "confirmed"
                    if st == "candidate":
                        best = "candidate"
            return best

    def id_label(self, ident: int) -> str:
        key = _fmt_id(ident)
        with self._lock:
            lab = (self.ids.get(key) or {}).get("label", "")
            names = [k for k, s in self.signals.items() if s.get("id") == key]
        if lab:
            return lab
        return ", ".join(CATALOG_BY_KEY[n][1] for n in names if n in CATALOG_BY_KEY)

    def bound_ids(self, statuses=("confirmed",)) -> List[int]:
        with self._lock:
            out = set()
            for spec in self.signals.values():
                if spec.get("status") in statuses and spec.get("id"):
                    try:
                        out.add(_parse_id(spec["id"]))
                    except ValueError:
                        pass
            return sorted(out)

    def catalog_rows(self, live: Optional[Dict[str, Any]] = None) -> List[dict]:
        """One row per catalog entry for the Assign tab."""
        live = live or {}
        rows = []
        with self._lock:
            for key, label, group, kind, target in SIGNAL_CATALOG:
                spec = self.signals.get(key) or {}
                binding = ""
                if spec.get("id"):
                    if spec.get("type", kind) == "bool":
                        binding = f"{spec['id']} b{int(spec.get('byte', 0))}.{int(spec.get('bit', 0))}"
                    else:
                        binding = f"{spec['id']} bytes {int(spec.get('start_byte', 0))}+{int(spec.get('length', 1))}"
                v = live.get(key)
                if isinstance(v, bool):
                    vtxt = "ON" if v else "off"
                elif isinstance(v, float):
                    vtxt = f"{v:.1f}"
                elif v is None:
                    vtxt = ""
                else:
                    vtxt = str(v)
                rows.append({
                    "key": key, "label": label, "group": group, "kind": kind, "target": target,
                    "status": spec.get("status", "unassigned") if spec else "unassigned",
                    "binding": binding, "id": spec.get("id", ""),
                    "byte": int(spec.get("byte", spec.get("start_byte", 0)) or 0),
                    "bit": int(spec.get("bit", 0) or 0),
                    "length": int(spec.get("length", 1) or 1),
                    "scale": float(spec.get("scale", 1.0) or 1.0),
                    "offset": float(spec.get("offset", 0.0) or 0.0),
                    "endian": spec.get("endian", "big"),
                    "live": vtxt,
                })
        # Bound first (confirmed, then candidate), catalog order within each —
        # on a 1280x800 sheet the rows that matter must not be below the fold.
        order = {"confirmed": 0, "candidate": 1, "unassigned": 2}
        rows.sort(key=lambda r: order.get(r["status"], 3))
        return rows

    def decode(self, store, statuses=("confirmed", "candidate")) -> Dict[str, Any]:
        """Current value of every bound signal from the frame store."""
        out: Dict[str, Any] = {}
        with self._lock:
            items = [(k, dict(s)) for k, s in self.signals.items() if s.get("status") in statuses and s.get("id")]
        cache: Dict[int, Optional[bytes]] = {}
        for key, spec in items:
            try:
                ident = _parse_id(spec["id"])
            except ValueError:
                continue
            if ident not in cache:
                last = store.last(ident)
                cache[ident] = last[0] if last else None
            data = cache[ident]
            if data is None:
                continue
            v = decode_one(spec, data)
            if v is not None:
                out[key] = v
        return out


_MAP: Optional[CanSignalMap] = None


def signal_map() -> CanSignalMap:
    global _MAP
    if _MAP is None:
        _MAP = CanSignalMap()
    return _MAP


# ── action analysis (Record tab) ──────────────────────────────────────────
def analyze_toggle(frames: List[Tuple[float, int, bytes]], t_base_end: float, t_act_start: float,
                   t_act_end: float, max_results: int = 20) -> List[dict]:
    """Rank (id, byte, bit) whose bit is steady in the baseline window and
    moves in the action window — the method that found the lights.

    frames: (t, id, data) in time order. Returns dicts with idHex, byte, bit,
    kind ('level' = held a new value, 'blink' = toggled regularly, 'pulse' =
    brief), transitions, baseline value, and a score.
    """
    per_id: Dict[int, List[Tuple[float, bytes]]] = {}
    for t, ident, data in frames:
        per_id.setdefault(ident, []).append((t, data))
    results = []
    for ident, lst in per_id.items():
        L = max(len(d) for _, d in lst)
        for b in range(min(L, 8)):
            for bit in range(8):
                prev = None
                base_val = None
                base_stable = True
                trans: List[Tuple[float, int]] = []
                for t, d in lst:
                    if b >= len(d):
                        continue
                    v = (d[b] >> bit) & 1
                    if prev is None:
                        prev = v
                        base_val = v
                        continue
                    if v != prev:
                        if t <= t_base_end:
                            base_stable = False
                            break
                        if t_act_start <= t <= t_act_end + 1.0:
                            trans.append((t, v))
                        prev = v
                if not base_stable or not trans:
                    continue
                n = len(trans)
                if n >= 6:
                    iv = [trans[i + 1][0] - trans[i][0] for i in range(n - 1)]
                    med = sorted(iv)[len(iv) // 2]
                    kind = "blink" if 0.15 <= med <= 1.5 else "noisy"
                elif n == 1 or (n == 2 and trans[1][0] - trans[0][0] > 0.5):
                    kind = "level"
                else:
                    kind = "pulse"
                if kind == "noisy":
                    continue
                first_at = trans[0][0] - t_act_start
                # Clean, prompt changes rank first: a bit that moved right
                # after the action started and then held (or blinked
                # regularly) beats one that fluttered late in the window.
                score = {"level": 3.0, "blink": 3.0, "pulse": 1.0}[kind]
                score -= min(max(first_at, 0.0), 10.0) / 10.0 * 2.0
                score -= 0.1 * min(n, 10) if kind != "blink" else 0.0
                results.append({
                    "id": ident, "idHex": f"{ident:03X}", "byte": b, "bit": bit, "kind": kind,
                    "transitions": n, "baseline": int(base_val or 0), "score": round(score, 2),
                    "firstAt": round(first_at, 1),
                })
    results.sort(key=lambda r: (-r["score"], r["firstAt"], r["id"], r["byte"], r["bit"]))
    return results[:max_results]


def merge_runs(runs: List[List[dict]], max_results: int = 20) -> List[dict]:
    """Combine analyze_toggle() results from repeated recordings of the same
    action: a bit that answered in every run outranks one that showed up
    once — the way to separate the light from the stalk's side effects."""
    if not runs:
        return []
    seen: Dict[Tuple[int, int, int], dict] = {}
    for res in runs:
        for r in res:
            k = (r["id"], r["byte"], r["bit"])
            e = seen.get(k)
            if e is None:
                e = dict(r)
                e["runs"] = 0
                e["scoreSum"] = 0.0
                seen[k] = e
            e["runs"] += 1
            e["scoreSum"] += float(r.get("score", 0.0))
    out = []
    for e in seen.values():
        e["score"] = round(e["scoreSum"] / e["runs"], 2)
        e["ofRuns"] = len(runs)
        del e["scoreSum"]
        out.append(e)
    out.sort(key=lambda r: (-r["runs"], -r["score"], r["id"], r["byte"], r["bit"]))
    return out[:max_results]
