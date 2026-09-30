"""Trip logging — opt-in, per-subsystem, silent, low-CPU.

Settings → Debugger → Trip logging. The user ticks channels (OBD, engine
simulation, CAN, power, a telemetry trace, alerts) and presses Start; each
subsystem's producer calls `trip_logger.log(channel, event, **fields)` and,
while recording with that channel active, the record is queued and appended to
`~/Documents/laladash_logs/trip_YYYYmmdd-HHMMSS.jsonl` by a background thread.
Pressing Stop closes the session. `trip_analyzer.summarize()` turns a file
back into a report (on-device sheet + `Tools/trip_report.py`).

Why it costs ~nothing:

- **Off = one set test.** `log()` returns immediately unless the channel is in
  `self._active` (an empty frozenset when stopped). Producers call it
  unconditionally; the whole feature is inert until Start.
- **Nothing on the Qt thread.** Records go to a `queue.Queue`; a daemon writer
  batches and appends, flushing ~1 s / N records — no `fsync` per line (SD
  wear + latency).
- **The telemetry trace is downsampled** to `sample_hz` (default 2 Hz, not the
  8 Hz feed) via a `TelemetryCoordinator` subscription that exists only while
  that channel records.
- **A size cap** stops the session before it can fill the SD.

JSON-lines so mixed channels/fields share one schema-free, streamable file.
"""
from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from typing import Optional, Set

from Modules.Runtime import settings_cache
from preferences import DATA_SETTINGS, TRIP_LOG_DIR

CHANNELS = ("obd", "engine", "can", "power", "telemetry", "alerts")
CHANNEL_LABELS = {
    "obd": "OBD", "engine": "Engine sim", "can": "CAN",
    "power": "Power", "telemetry": "Telemetry trace", "alerts": "Alerts",
}

# Telemetry-trace fields: output name → snapshot keys to try (first numeric
# one wins; missing keys are simply skipped, per the snapshot's tolerance).
_TRACE_FIELDS = [
    ("rpm",      ("RPM", "RPM_smooth")),
    ("speed",    ("Speed", "Vehicle Speed")),
    ("coolant",  ("Coolant", "Coolant Temp")),
    ("throttle", ("Throttle Pos", "Throttle")),
    ("iat",      ("Intake Air", "Intake Air Temp", "IAT")),
    ("load",     ("Engine Load", "Absolute Load", "Load")),
    ("boost",    ("Boost", "Boost Pressure", "MAP")),
    ("timing",   ("Timing Adv", "Timing Advance")),
    ("fuel_p",   ("Fuel Pressure",)),
    ("batt_v",   ("Battery", "Control Module Voltage", "ELM Voltage")),
    ("gear",     ("Gear",)),
]

_FLUSH_INTERVAL_S = 1.0
_QUEUE_MAX = 20000            # drop-guard: a runaway producer can't grow RAM unboundedly


def _num(entry):
    """A finite float from a snapshot entry (dict with 'value', or a number)."""
    v = entry.get("value") if isinstance(entry, dict) else entry
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class TripLogger:
    _instance: Optional["TripLogger"] = None

    @classmethod
    def instance(cls) -> "TripLogger":
        if cls._instance is None:
            cls._instance = TripLogger()
        return cls._instance

    def __init__(self) -> None:
        self._active: Set[str] = frozenset()     # empty = not recording
        self._q: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAX)
        self._writer: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._path = ""
        self._t0 = 0.0
        self._sample_hz = 2.0
        self._last_sample = 0.0
        self._subscribed = False
        # cheap counters (writer thread owns bytes/records; read is best-effort)
        self.bytes_written = 0
        self.record_count = 0
        self._max_bytes = 20 * 1024 * 1024
        self._capped = False

    # ── config ────────────────────────────────────────────────────────────
    @staticmethod
    def config_channels() -> Set[str]:
        raw = settings_cache.get("TripLog", "channels", fallback="", path=DATA_SETTINGS) or ""
        return {c.strip() for c in raw.split(",") if c.strip() in CHANNELS}

    @staticmethod
    def set_config_channels(chans) -> None:
        keep = [c for c in CHANNELS if c in set(chans)]
        settings_cache.set_value("TripLog", "channels", ",".join(keep), path=DATA_SETTINGS)

    @staticmethod
    def config_sample_hz() -> float:
        try:
            return max(0.5, min(10.0, settings_cache.getfloat("TripLog", "sample_hz", fallback=2.0, path=DATA_SETTINGS)))
        except Exception:
            return 2.0

    # ── lifecycle ───────────────────────────────────────────────────────────
    @property
    def recording(self) -> bool:
        return bool(self._active)


    @property
    def session_path(self) -> str:
        return self._path

    @property
    def capped(self) -> bool:
        return self._capped

    def start(self, active, header: Optional[dict] = None) -> bool:
        """Begin a session with the given channel set. Returns False on error
        or if already recording / no channels."""
        if self.recording:
            return False
        active = frozenset(c for c in active if c in CHANNELS)
        if not active:
            return False
        try:
            os.makedirs(TRIP_LOG_DIR, exist_ok=True)
        except OSError as exc:
            print(f"[triplog] cannot make {TRIP_LOG_DIR}: {exc}", flush=True)
            return False
        name = "trip_" + time.strftime("%Y%m%d-%H%M%S") + ".jsonl"
        self._path = os.path.join(TRIP_LOG_DIR, name)
        self._sample_hz = self.config_sample_hz()
        try:
            self._max_bytes = max(1, int(settings_cache.getint(
                "TripLog", "max_mb", fallback=20, path=DATA_SETTINGS))) * 1024 * 1024
        except Exception:
            self._max_bytes = 20 * 1024 * 1024
        self.bytes_written = 0
        self.record_count = 0
        self._capped = False
        self._last_sample = 0.0
        self._t0 = time.monotonic()
        self._stop.clear()
        # drain any stale records from a previous run
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._writer = threading.Thread(target=self._run, daemon=True, name="TripLogWriter")
        self._writer.start()
        self._active = active
        # header record first
        hdr = {"ch": "meta", "ev": "header", "channels": sorted(active),
               "sample_hz": self._sample_hz, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        if header:
            hdr.update(header)
        self._enqueue({"t": 0.0, **hdr})
        if "telemetry" in active:
            self._subscribe()
        print(f"[triplog] recording {sorted(active)} → {name}", flush=True)
        return True

    def stop(self) -> None:
        if not self.recording:
            return
        self._unsubscribe()
        self._active = frozenset()
        self._enqueue({"t": round(time.monotonic() - self._t0, 3), "ch": "meta", "ev": "end",
                       "records": self.record_count})
        self._stop.set()
        w = self._writer
        if w is not None and w.is_alive():
            w.join(timeout=3.0)
        self._writer = None
        print(f"[triplog] stopped ({self.record_count} records, {self.bytes_written} bytes)", flush=True)

    # ── the log call (hot path — keep cheap) ────────────────────────────────
    def log(self, channel: str, event: str, **fields) -> None:
        active = self._active
        if channel not in active:
            return
        rec = {"t": round(time.monotonic() - self._t0, 3), "ch": channel, "ev": event}
        if fields:
            rec.update(fields)
        self._enqueue(rec)

    def _enqueue(self, rec: dict) -> None:
        try:
            self._q.put_nowait(rec)
        except queue.Full:
            pass  # drop rather than block a producer / grow RAM

    # ── telemetry trace ─────────────────────────────────────────────────────
    def _subscribe(self) -> None:
        if self._subscribed:
            return
        try:
            from Modules.Runtime.telemetry_coordinator import TelemetryCoordinator
            TelemetryCoordinator.instance().subscribe(self._on_snap)
            self._subscribed = True
        except Exception as exc:
            print(f"[triplog] telemetry subscribe failed: {exc}", flush=True)

    def _unsubscribe(self) -> None:
        if not self._subscribed:
            return
        try:
            from Modules.Runtime.telemetry_coordinator import TelemetryCoordinator
            TelemetryCoordinator.instance().unsubscribe(self._on_snap)
        except Exception:
            pass
        self._subscribed = False

    def _on_snap(self, snap: dict) -> None:
        # Runs on the coordinator thread. Downsample, extract a small numeric
        # subset, enqueue. Cheap; no disk here.
        if "telemetry" not in self._active or not isinstance(snap, dict):
            return
        now = time.monotonic()
        if now - self._last_sample < (1.0 / self._sample_hz):
            return
        self._last_sample = now
        fields = {}
        for out, keys in _TRACE_FIELDS:
            for k in keys:
                if k in snap:
                    v = _num(snap[k])
                    if v is not None:
                        fields[out] = round(v, 3)
                        break
        if fields:
            self._enqueue({"t": round(now - self._t0, 3), "ch": "telemetry", "ev": "s", **fields})

    # ── writer thread ────────────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            f = open(self._path, "w", encoding="utf-8", buffering=1 << 16)
        except OSError as exc:
            print(f"[triplog] open failed: {exc}", flush=True)
            return
        pending = 0
        last_flush = time.monotonic()
        try:
            while not self._stop.is_set() or not self._q.empty():
                try:
                    rec = self._q.get(timeout=0.5)
                except queue.Empty:
                    if pending:
                        f.flush()
                        pending = 0
                    continue
                line = json.dumps(rec, separators=(",", ":")) + "\n"
                f.write(line)
                self.bytes_written += len(line)
                self.record_count += 1
                pending += 1
                now = time.monotonic()
                if pending >= 200 or (now - last_flush) >= _FLUSH_INTERVAL_S:
                    f.flush()
                    pending = 0
                    last_flush = now
                if self.bytes_written >= self._max_bytes and not self._capped:
                    self._capped = True
                    f.write(json.dumps({"t": round(now - self._t0, 3), "ch": "meta",
                                        "ev": "capped", "max_mb": self._max_bytes // (1024 * 1024)}) + "\n")
                    # stop accepting more; drain what is queued, then end
                    self._active = frozenset()
                    self._unsubscribe()
                    self._stop.set()
                    print("[triplog] size cap reached — recording stopped", flush=True)
        finally:
            try:
                f.flush()
                os.fsync(f.fileno())
                f.close()
            except Exception:
                pass


# ── module-level convenience for producers ──────────────────────────────────
def log(channel: str, event: str, **fields) -> None:
    TripLogger.instance().log(channel, event, **fields)


def _selftest() -> int:
    import tempfile
    import preferences
    d = tempfile.mkdtemp()
    preferences.TRIP_LOG_DIR = d
    import Modules.Runtime.trip_logger as m
    m.TRIP_LOG_DIR = d
    tl = TripLogger()
    # no-op when stopped
    tl.log("obd", "connect", foo=1)
    assert tl._q.qsize() == 0, "log wrote while stopped"
    ok = tl.start({"obd", "engine"})
    assert ok
    for i in range(50):
        tl.log("obd", "value", rpm=i * 100)
        tl.log("engine", "feed", rpm=i * 100, latency_ms=42)
        tl.log("can", "fps", fps=30)   # inactive channel → dropped
    time.sleep(0.3)
    tl.stop()
    lines = open(tl.session_path, encoding="utf-8").read().splitlines()
    recs = [json.loads(l) for l in lines]
    kinds = {}
    for r in recs:
        kinds[r["ch"]] = kinds.get(r["ch"], 0) + 1
    assert kinds.get("meta", 0) >= 2, kinds          # header + end
    assert kinds.get("obd") == 50 and kinds.get("engine") == 50, kinds
    assert "can" not in kinds, "inactive channel leaked"
    print("trip_logger selftest ok:", kinds, "->", tl.session_path)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
