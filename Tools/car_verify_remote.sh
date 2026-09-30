#!/bin/bash
# Car check with the IGNITION ON, ENGINE OFF — runs on the Pi itself (no SSH
# round-trips inside, so a weak Wi-Fi link cannot stall it):
#   ssh pi "~/Desktop/LaLaDash/Tools/car_verify_remote.sh"
# Proves OBD connect, the STN session, CAN frames and the 0x5DE beam mapping.
# Needs LALADASH_DEBUG=1 on the unit (debug probe). See POST_GUI_CHECKS.md 2026-09-16.
cd ~/Desktop/LaLaDash || exit 1
export XDG_RUNTIME_DIR=/run/user/1000
P="python3 Tools/probe_client.py"
LOG=~/Desktop/LaLaDash_ERROR.txt
t0=$(date +%s); say() { printf '[%3ds %s] %s\n' "$(( $(date +%s) - t0 ))" "$(date +%H:%M:%S)" "$*"; }

say "verify start"
for i in $(seq 1 60); do grep -aq "debug_probe\] listening" "$LOG" && break; sleep 2; done
ok=0
for i in $(seq 1 90); do
    c=$($P eval 'OBD.connected' 2>/dev/null)
    [ "$c" = "OK true" ] && { ok=1; break; }
    sleep 1
done
say "OBD connected=$ok"
grep -aE "\[obd\]|\[OBD\]|\[can\]" "$LOG" | tail -5 | cut -c1-200
sleep 12
say "telemetry: $(grep -a '\[telemetry\]' "$LOG" | tail -1 | cut -c1-200)"
say "CAN: $($P eval '"fps " + Can.fps + " ids " + Can.uniqueIds + " duty " + Can.dutyPercent' 2>/dev/null)"
say "lights now: $($P eval 'Telemetry.activeDashLightKeys.join(",")' 2>/dev/null)"
say ">>> LIGHTS TEST: toggle LOW BEAM on/off, then HIGH BEAM, then HAZARDS — 45 s"
for i in $(seq 1 15); do
    sleep 3
    say "lights: $($P eval 'Telemetry.activeDashLightKeys.join(",")' 2>/dev/null)  alerts: $($P eval 'Alerts.model.count' 2>/dev/null)"
done
$P page 1 >/dev/null 2>&1; sleep 2
$P grab /tmp/dash_ign.png >/dev/null 2>&1 && say "shot /tmp/dash_ign.png"
$P page 0 >/dev/null 2>&1
say "verify done"
