#!/bin/bash
# The ENGINE window — runs on the Pi, detached:
#   ssh pi "setsid nohup ~/Desktop/LaLaDash/Tools/car_window_remote.sh 80 > /tmp/window.out 2>&1 &"
# Waits for OBD + the STN session, starts an 80 s raw CAN capture, takes two
# Dashboard grabs and samples the lit dash lights; the driver follows a fixed
# clock schedule from engine start (POST_GUI_CHECKS.md 2026-09-16). Output in
# /tmp/window.out, log excerpt in /tmp/window_log.txt, capture under Data/can_captures/live_*.
cd ~/Desktop/LaLaDash || exit 1
export XDG_RUNTIME_DIR=/run/user/1000
P="python3 Tools/probe_client.py"
LOG=~/Desktop/LaLaDash_ERROR.txt
SECS="${1:-75}"
t0=$(date +%s); say() { printf '[%3ds %s] %s\n' "$(( $(date +%s) - t0 ))" "$(date +%H:%M:%S)" "$*"; }

say "window start"
ok=0
for i in $(seq 1 240); do
    c=$($P eval 'OBD.connected' 2>/dev/null)
    [ "$c" = "OK true" ] && { ok=1; break; }
    sleep 1
done
say "OBD connected=$ok"

can=0
for i in $(seq 1 20); do
    if grep -aq "UART 2000000\|UART 1000000\|UART 921600" "$LOG"; then can=1; break; fi
    if grep -aq "session setup failed\|not an STN" "$LOG"; then break; fi
    sleep 1
done
say "STN session=$can"

if [ $can -eq 1 ]; then
    $P eval 'Can.open()' >/dev/null 2>&1
    sleep 1
    $P eval "Can.startRawCapture($SECS)" >/dev/null 2>&1
    say ">>> GO — capture running for $SECS s"
else
    say ">>> GO — no CAN session, passive checks only"
fi

sleep 15; $P grab /tmp/dash.png >/dev/null 2>&1 && say "shot dash"
sleep 15; $P grab /tmp/dash2.png >/dev/null 2>&1 && say "shot dash2"
sleep 12; say "lights@42s: $($P eval 'Telemetry.activeDashLightKeys.join(",")' 2>/dev/null)"
sleep 12; say "lights@54s: $($P eval 'Telemetry.activeDashLightKeys.join(",")' 2>/dev/null)"
sleep 6;  say "lights@60s: $($P eval 'Telemetry.activeDashLightKeys.join(",")' 2>/dev/null)"
$P stats > /tmp/window_stats.json 2>/dev/null

if [ $can -eq 1 ]; then
    for i in $(seq 1 $((SECS + 10))); do
        grep -aq "raw capture:" "$LOG" && break
        sleep 1
    done
    $P eval 'Can.close()' >/dev/null 2>&1
fi
say "window done"
grep -aE "raw capture|\[can\]|\[engine\]|\[telemetry\]|\[obd\]|\[power\]|\[bluetooth\]" "$LOG" | tail -30 | cut -c1-240 > /tmp/window_log.txt
say "log saved"
