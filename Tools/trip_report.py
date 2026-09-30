#!/usr/bin/env python3
"""Read a LaLaDash trip log (Settings → Debugger → Trip logging) as a report.

    python3 Tools/trip_report.py ~/Documents/laladash_logs/trip_….jsonl
    python3 Tools/trip_report.py trip_….jsonl --plot     # RPM/throttle/latency vs time

Pull the .jsonl off the Pi first (it lives in ~/Documents/laladash_logs/,
outside the repo — `scp` it, or open the car's Documents folder). Uses the
same `Modules/Runtime/trip_analyzer.summarize` the on-device report sheet does.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Modules.Runtime.trip_analyzer import summarize, human_size  # noqa: E402


def _print_report(path: str) -> None:
    rep = summarize(path)
    if rep.get("error"):
        print(f"error: {rep['error']}")
        return
    print(f"== {rep['name']}  ·  {rep['records']} records  ·  {human_size(rep['size'])}")
    dur = rep.get("duration_s", 0)
    print(f"   duration {int(dur // 60)}m {int(dur % 60)}s   channels: {', '.join(rep['channels'])}")
    h = rep.get("header") or {}
    if h:
        print(f"   header: {json.dumps({k: v for k, v in h.items() if k != 'channels'})}")
    for sec in rep["sections"]:
        span = f" · {sec['span_s']}s" if sec.get("span_s") else ""
        print(f"\n-- {sec['label']}  ({sec['count']}{span})")
        for m in sec.get("metrics", []):
            print(f"     {m['field']:<12} min {m['min']:<10} avg {m['avg']:<10} "
                  f"max {m['max']:<10} last {m['last']}   n={m['n']}")
        if sec.get("events"):
            print("     events: " + "   ".join(f"{k} ×{v}" for k, v in sec["events"].items()))


def _plot(path: str) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed (pip install matplotlib) — skipping --plot")
        return
    # collect telemetry + engine series
    series: dict = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ch") not in ("telemetry", "engine"):
                continue
            t = r.get("t")
            for k, v in r.items():
                if k in ("ch", "ev", "t") or not isinstance(v, (int, float)) or isinstance(v, bool):
                    continue
                xs, ys = series.setdefault(f"{r['ch']}.{k}", ([], []))
                xs.append(t); ys.append(v)
    if not series:
        print("no numeric telemetry/engine series to plot")
        return
    fig, ax = plt.subplots(figsize=(11, 6))
    for name, (xs, ys) in sorted(series.items()):
        ax.plot(xs, ys, label=name, linewidth=1)
    ax.set_xlabel("t (s)")
    ax.set_title(os.path.basename(path))
    ax.legend(loc="upper right", fontsize=8, ncol=2)
    ax.grid(True, alpha=0.3)
    out = os.path.splitext(path)[0] + ".png"
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    print(f"plot → {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file")
    ap.add_argument("--plot", action="store_true", help="also save an RPM/throttle/latency plot")
    a = ap.parse_args()
    if not os.path.isfile(a.file):
        print(f"no such file: {a.file}")
        return 2
    _print_report(a.file)
    if a.plot:
        _plot(a.file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
