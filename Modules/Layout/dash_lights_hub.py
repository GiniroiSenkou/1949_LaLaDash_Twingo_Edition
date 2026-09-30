# Modules/dash_lights_hub.py
from __future__ import annotations
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

# Every dash light the app knows how to draw, grouped the way the Lights
# Simulator (Settings > Debugger) lays them out. The key is the icon file in
# GUI/Icons/Dash/ and the name the camera detector / CAN signal map emit;
# main._DASH_LIGHT_ALERTS maps a subset of these to alerts.
#   (key, label, group)
DASH_LIGHT_CATALOG: List[Tuple[str, str, str]] = [
    ("engine",              "Engine",            "Warnings"),
    ("oil",                 "Oil pressure",      "Warnings"),
    ("battery",             "Battery",           "Warnings"),
    ("coolant_temperature", "Coolant temp",      "Warnings"),
    ("stop",                "STOP",              "Warnings"),
    ("abs",                 "ABS",               "Warnings"),
    ("airbag",              "Airbag",            "Warnings"),
    ("steering_fault",      "Steering",          "Warnings"),
    ("efl",                 "EFL",               "Warnings"),
    ("tyres",               "Tyres",             "Warnings"),
    ("petrol",              "Low fuel",          "Warnings"),
    ("low_beam",            "Low beam",          "Exterior lights"),
    ("high_beam",           "High beam",         "Exterior lights"),
    ("position_lights",     "Position lights",   "Exterior lights"),
    ("fog_lights_front",    "Front fog",         "Exterior lights"),
    ("fog_lights_rear",     "Rear fog",          "Exterior lights"),
    ("left_turn",           "Turn left",         "Exterior lights"),
    ("right_turn",          "Turn right",        "Exterior lights"),
    ("doors_open",          "Door open",         "Body & driver"),
    ("seatbelt",            "Seatbelt",          "Body & driver"),
    ("parking",             "Handbrake",         "Body & driver"),
    ("eco",                 "Eco mode",          "Systems"),
    ("speed_limiter",       "Speed limiter",     "Systems"),
    ("traction_control",    "Traction control",  "Systems"),
    ("autostart_enable",    "Stop & Start on",   "Systems"),
    ("autostart_disable",   "Stop & Start off",  "Systems"),
]

# Ready-made sets for the simulator's one-tap buttons.
DASH_LIGHT_PRESETS: Dict[str, List[str]] = {
    "night":    ["low_beam", "position_lights"],
    "warnings": ["engine", "oil", "battery", "coolant_temperature"],
    "critical": ["stop", "oil", "airbag", "steering_fault", "efl"],
    "body":     ["doors_open", "seatbelt", "parking"],
    "hazard":   ["left_turn", "right_turn"],
}


class DashLightsHub:
    """Lightweight singleton used to broadcast active dash-lights to any page (e.g., Dashboard)."""
    _inst: Optional["DashLightsHub"] = None

    def __init__(self):
        self._detected_active: List[str] = []   # camera detector
        self._can_active: List[str] = []        # CAN signal map (Modules/Obd/can_feed.py)
        self._active: List[str] = []
        self._overrides: Dict[str, bool] = {}
        self._listeners: List[Callable[[List[str]], None]] = []

    @classmethod
    def instance(cls) -> "DashLightsHub":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def get_active(self) -> List[str]:
        return list(self._active)

    def get_detected_active(self) -> List[str]:
        return list(self._detected_active)

    def get_overrides(self) -> Dict[str, bool]:
        return dict(self._overrides)

    def get_override(self, key: str) -> Optional[bool]:
        if key not in self._overrides:
            return None
        return bool(self._overrides.get(key))

    def set_override(self, key: str, state: Optional[bool]) -> None:
        k = str(key or "").strip()
        if not k:
            return
        if state is None:
            self._overrides.pop(k, None)
        else:
            self._overrides[k] = bool(state)
        self._recompute_and_notify()

    def clear_overrides(self) -> None:
        if not self._overrides:
            return
        self._overrides.clear()
        self._recompute_and_notify()

    def _effective_from_detected(self) -> List[str]:
        seen: Set[str] = set()
        out: List[str] = []
        # Camera-detected first, then CAN-decoded: the two sources are
        # unioned, so the car (no camera) and the bench (no bus) both work.
        for k in list(self._detected_active) + list(self._can_active):
            if k in seen:
                continue
            seen.add(k)
            out.append(k)
        for k, state in self._overrides.items():
            if state:
                if k not in seen:
                    seen.add(k)
                    out.append(k)
            elif k in seen:
                seen.remove(k)
                out = [x for x in out if x != k]
        return out

    def _recompute_and_notify(self) -> None:
        new_list = self._effective_from_detected()
        if new_list == self._active:
            return
        self._active = new_list
        for cb in list(self._listeners):
            try:
                cb(self._active)
            except Exception:
                pass

    def set_active(self, keys: Iterable[str]) -> None:
        # Maintain order, drop duplicates
        seen: Set[str] = set()
        new_list: List[str] = []
        for k in keys:
            if k in seen:
                continue
            seen.add(k)
            new_list.append(k)

        if new_list == self._detected_active:
            return
        self._detected_active = new_list
        self._recompute_and_notify()

    def set_can_active(self, keys: Iterable[str]) -> None:
        """Lights decoded from the CAN bus; same semantics as set_active()."""
        seen: Set[str] = set()
        new_list: List[str] = []
        for k in keys:
            if k in seen:
                continue
            seen.add(k)
            new_list.append(k)
        if new_list == self._can_active:
            return
        self._can_active = new_list
        self._recompute_and_notify()

    def get_can_active(self) -> List[str]:
        return list(self._can_active)

    def subscribe(self, callback: Callable[[List[str]], None]) -> None:
        """Register a listener. It gets called immediately with the current state."""
        self._listeners.append(callback)
        try:
            callback(self._active)
        except Exception:
            pass

    def unsubscribe(self, callback: Callable[[List[str]], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass
