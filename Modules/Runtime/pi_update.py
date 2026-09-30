"""Raspberry Pi package update, run in the background with a progress fraction.

Settings → System → Update Raspberry Pi runs, in order:

    sudo -n apt-get update
    sudo -n apt-get -y full-upgrade
    sudo -n apt-get -y autoclean

on a worker thread. `-n` (non-interactive sudo) so it fails *fast* on a box
without passwordless sudo instead of hanging on a hidden password prompt —
Raspberry Pi OS gives the default user NOPASSWD sudo, so on the Pi it just
runs (the same reason `sudo reboot` works from the app).

Progress comes from apt's own machine-readable status stream
(`-o APT::Status-Fd=<fd>`): apt writes `dlstatus:<id>:<pct>:<desc>` while
downloading and `pmstatus:<pkg>:<pct>:<desc>` while installing, `<pct>` being
0..100 over the whole operation. Each stage owns a slice of the bar
(`STAGES` weights, summing to 1); within a stage the bar tracks that percent,
so the overall fraction only ever moves forward.

There is deliberately **no cancel**: interrupting apt mid-upgrade is how a
system is left half-configured. The run lives on this thread and the bridge
holds the state, so it keeps going and the bar resumes if the page is left.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Callable, List, Tuple

# (label, args after `apt-get`, weight). Weights sum to 1.0 — the download +
# unpack + configure of a full-upgrade is the overwhelming majority.
STAGES: List[Tuple[str, List[str], float]] = [
    ("Refreshing package lists", ["update"], 0.20),
    ("Upgrading packages",
     ["-y", "-o", "Dpkg::Options::=--force-confdef",
      "-o", "Dpkg::Options::=--force-confold", "full-upgrade"], 0.72),
    ("Cleaning up", ["-y", "autoclean"], 0.08),
]


def available() -> bool:
    return shutil.which("apt-get") is not None and shutil.which("sudo") is not None


class PiUpdater:
    """Runs `STAGES` on a daemon thread, reporting through three callbacks.

    on_progress(frac: float, stage: str) — frac 0..1, monotonic
    on_line(line: str)                    — the latest line of apt output
    on_done(ok: bool, message: str)       — once, when the run ends
    All three fire from the worker thread; the caller marshals to its own.
    """

    def __init__(self,
                 on_progress: Callable[[float, str], None],
                 on_line: Callable[[str], None],
                 on_done: Callable[[bool, str], None]) -> None:
        self._on_progress = on_progress
        self._on_line = on_line
        self._on_done = on_done
        self._thread: "threading.Thread | None" = None
        self.running = False
        self._last_line = ""

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="PiUpdate")
        self._thread.start()

    # ── worker ────────────────────────────────────────────────────────────
    def _run(self) -> None:
        done = 0.0
        try:
            for label, args, weight in STAGES:
                self._on_progress(done, label)
                rc = self._run_stage(label, args, done, weight)
                if rc != 0:
                    self._finish(False, self._explain(label, rc))
                    return
                done += weight
                self._on_progress(done, label)
            self._finish(True, "Up to date — a reboot may be needed for a new kernel")
        except FileNotFoundError:
            self._finish(False, "apt-get or sudo not found")
        except Exception as exc:
            self._finish(False, f"{type(exc).__name__}: {exc}")

    def _run_stage(self, label: str, args: List[str], base: float, weight: float) -> int:
        r, w = os.pipe()
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        if os.environ.get("LALADASH_PIUPDATE_DEMO"):
            # Rehearse the bar without touching the system (dev PC / on-device
            # smoke test): a short script that emits real status-fd percents.
            demo = ('for p in 0 20 40 60 80 100; do echo "pmstatus:demo:$p:'
                    + label + '" >&' + str(w) + '; echo "demo line $p"; sleep 0.5; done; exit 0')
            cmd = ["/bin/bash", "-c", demo]
        else:
            cmd = ["sudo", "-n", "apt-get", "-o", f"APT::Status-Fd={w}", *args]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=env, pass_fds=(w,))
        except Exception:
            os.close(r)
            os.close(w)
            raise
        os.close(w)   # the child holds the write end now

        def _status() -> None:
            try:
                with os.fdopen(r, "r", buffering=1) as f:
                    for line in f:
                        pct = self._parse_pct(line)
                        if pct is not None:
                            self._on_progress(base + weight * pct / 100.0, label)
            except Exception:
                pass

        t = threading.Thread(target=_status, daemon=True, name="PiUpdateStatus")
        t.start()
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self._last_line = line
                self._on_line(line)
        proc.wait()
        t.join(timeout=1.0)
        return proc.returncode

    @staticmethod
    def _parse_pct(line: str):
        # dlstatus:<id>:<pct>:<desc>  /  pmstatus:<pkg>:<pct>:<desc>
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] in ("dlstatus", "pmstatus"):
            try:
                return max(0.0, min(100.0, float(parts[2])))
            except ValueError:
                return None
        return None

    def _explain(self, label: str, rc: int) -> str:
        low = self._last_line.lower()
        if "password is required" in low or "a terminal is required" in low or "not allowed" in low:
            return "Needs passwordless sudo — run this on the Pi"
        if "could not get lock" in low or "unable to lock" in low:
            return "apt is busy — another update is running"
        if "temporary failure resolving" in low or "failed to fetch" in low or "no route" in low:
            return "No internet — check the connection"
        tail = (" · " + self._last_line[-80:]) if self._last_line else ""
        return f"{label} failed (exit {rc}){tail}"

    def _finish(self, ok: bool, message: str) -> None:
        self.running = False
        self._on_done(ok, message)


if __name__ == "__main__":
    # Dry-run the plumbing against a harmless command (no apt, no sudo):
    #   python3 -m Modules.Runtime.pi_update
    done = threading.Event()
    _script = ('fd=$(echo "$@" | grep -oP "APT::Status-Fd=\\K[0-9]+"); '
               'for p in 0 40 100; do echo "pmstatus:demo:$p:Working" >&$fd; '
               'echo "line $p"; done; exit 0')
    _orig = subprocess.Popen

    def _fake(cmd, **kw):
        return _orig(["/bin/bash", "-c", _script, "bash"] + cmd[2:], **kw)

    subprocess.Popen = _fake   # type: ignore[assignment]
    STAGES[:] = [("Stage one", ["update"], 0.5), ("Stage two", ["full-upgrade"], 0.5)]
    up = PiUpdater(lambda f, s: print(f"progress {f:.2f} {s}"),
                   lambda l: print("  |", l[:60]),
                   lambda ok, m: (print("DONE", ok, m), done.set()))
    up.start()
    done.wait(5)
