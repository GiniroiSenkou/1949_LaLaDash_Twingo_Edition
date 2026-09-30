"""Turn a trip-log `.jsonl` (see trip_logger.py) into a compact report.

`summarize(path)` streams the file once and returns a dict the on-device
report sheet renders and `Tools/trip_report.py` prints:

    {
      "name": "trip_….jsonl", "size": <bytes>, "records": N,
      "header": {…}, "duration_s": <float>, "channels": ["obd", …],
      "sections": [
        {"channel": "telemetry", "label": "Telemetry trace", "count": N,
         "span_s": …, "metrics": [{"field":"rpm","min":…,"max":…,"avg":…,
                                    "last":…,"n":…}, …]},
        {"channel": "obd", "count": N, "events": {"connect":1,"error":2,…},
         "timeline": [{"t":…,"ev":"connect","…":…}, …]},
        …
      ],
    }

Pure Python, no Qt — shared by the bridge (off the Qt thread) and the tool.
"""
from __future__ import annotations

import json
import os
from typing import Optional


# Avoid importing the whole logger (and its threads / Qt-adjacent deps) just
# for labels; keep a local copy.
CHANNEL_LABELS = {
    "obd": "OBD", "engine": "Engine sim", "can": "CAN",
    "power": "Power", "telemetry": "Telemetry trace", "alerts": "Alerts", "meta": "Session",
}

_TIMELINE_MAX = 40           # first/last few notable events per event-channel
_NUMERIC_SKIP = {"t"}        # never treat the timestamp as a metric


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def summarize(path: str) -> dict:
    name = os.path.basename(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0

    header: dict = {}
    total = 0
    t_min: Optional[float] = None
    t_max: Optional[float] = None
    # per channel
    counts: dict = {}
    spans: dict = {}
    # numeric accumulators: channel → field → [n, sum, min, max, last]
    numeric: dict = {}
    # event tallies + timeline for non-numeric channels
    events: dict = {}
    timelines: dict = {}

    try:
        f = open(path, encoding="utf-8")
    except OSError as exc:
        return {"name": name, "size": size, "error": str(exc), "records": 0, "sections": []}

    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ch = str(r.get("ch", "?"))
            ev = str(r.get("ev", ""))
            t = r.get("t")
            if ch == "meta":
                if ev == "header":
                    header = {k: v for k, v in r.items() if k not in ("ch", "ev", "t")}
                continue
            total += 1
            counts[ch] = counts.get(ch, 0) + 1
            if _is_number(t):
                t_min = t if t_min is None else min(t_min, t)
                t_max = t if t_max is None else max(t_max, t)
                sp = spans.setdefault(ch, [t, t])
                sp[0] = min(sp[0], t); sp[1] = max(sp[1], t)
            # numeric fields → stats
            had_numeric = False
            for k, v in r.items():
                if k in ("ch", "ev") or k in _NUMERIC_SKIP:
                    continue
                if _is_number(v):
                    had_numeric = True
                    acc = numeric.setdefault(ch, {}).get(k)
                    if acc is None:
                        numeric[ch][k] = [1, float(v), float(v), float(v), float(v)]
                    else:
                        acc[0] += 1; acc[1] += v
                        acc[2] = min(acc[2], v); acc[3] = max(acc[3], v); acc[4] = float(v)
            # event tally
            evbag = events.setdefault(ch, {})
            evbag[ev] = evbag.get(ev, 0) + 1
            # timeline for event-ish channels (skip the dense telemetry sampler)
            if ch != "telemetry":
                tl = timelines.setdefault(ch, [])
                if len(tl) < _TIMELINE_MAX * 2:
                    tl.append(r)
                else:
                    tl[_TIMELINE_MAX + (len(tl) % _TIMELINE_MAX)] = r  # ring the tail

    duration = round((t_max - t_min), 1) if (t_min is not None and t_max is not None) else 0.0

    sections = []
    for ch in [c for c in ("telemetry", "obd", "engine", "can", "power", "alerts") if c in counts] \
            + [c for c in counts if c not in ("telemetry", "obd", "engine", "can", "power", "alerts")]:
        sp = spans.get(ch, [0, 0])
        sec = {"channel": ch, "label": CHANNEL_LABELS.get(ch, ch.title()),
               "count": counts[ch], "span_s": round(sp[1] - sp[0], 1)}
        metrics = []
        for field, acc in sorted(numeric.get(ch, {}).items()):
            n, s, mn, mx, last = acc
            metrics.append({"field": field, "n": n, "min": round(mn, 2),
                            "max": round(mx, 2), "avg": round(s / n, 2), "last": round(last, 2)})
        if metrics:
            sec["metrics"] = metrics
        evb = {k: v for k, v in events.get(ch, {}).items() if k}
        if evb and ch != "telemetry":
            sec["events"] = evb
        tl = timelines.get(ch)
        if tl and ch not in ("telemetry", "engine"):
            head = tl[:8]
            tail = tl[-8:] if len(tl) > 8 else []
            sec["timeline"] = head + ([{"gap": True}] if tail and len(tl) > 16 else []) + tail
        sections.append(sec)

    return {
        "name": name, "size": size, "records": total, "header": header,
        "duration_s": duration, "channels": sorted(counts.keys()),
        "sections": sections,
    }


def human_size(n) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"
