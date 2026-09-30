#!/bin/bash
# Action cam probe — run ON THE PI with the camera attached / powered.
#
#   Tools/actioncam_probe.sh usb            GoPro on a USB cable (GoPro Connect mode)
#   Tools/actioncam_probe.sh uvc [match]    Insta360 (or any UVC cam) in Webcam mode
#   Tools/actioncam_probe.sh wifi [iface]   GoPro / Insta360 over its own Wi-Fi (already joined)
#
# Writes what the app needs to know about this camera to ~/actioncam_probe.txt
# (+ ~/actioncam_frame.jpg when a picture came through): the USB network and
# the camera's address, /gopro/camera/state, which settings ids it reports,
# whether the webcam / preview UDP stream arrives, or the UVC node and its
# formats. Everything goes through Modules/Runtime/actioncam.py — the same
# code the ActionCam page runs.
set -u
cd "$(dirname "$0")/.." || exit 1
MODE="${1:-usb}"; ARG="${2:-}"
REPORT="$HOME/actioncam_probe.txt"; FRAME="$HOME/actioncam_frame.jpg"
: > "$REPORT"
say() { printf '%s\n' "$*" | tee -a "$REPORT"; }
run() { say "\$ $*"; "$@" 2>&1 | tee -a "$REPORT"; }
say "== action cam probe $(date '+%F %T') on $(hostname) mode=$MODE"

gopro_probe() {  # $1 = camera ip
    local IP="$1" B="http://$1:8080"
    say ""; say "-- Open GoPro at $B"
    run curl -m 4 -sS "$B/gopro/camera/control/wired_usb?p=1"; say ""
    say '$ state'
    curl -m 4 -sS "$B/gopro/camera/state" | tee "$HOME/actioncam_state.json" | head -c 600 | tee -a "$REPORT"; say ""
    python3 - "$HOME/actioncam_state.json" <<'EOF' 2>&1 | tee -a "$REPORT"
import json, sys
from Modules.Runtime.actioncam import gopro_summary, gopro_settings_model
d = json.load(open(sys.argv[1]))
print("summary:", gopro_summary(d))
rows = gopro_settings_model(d)
print("settings the page will offer:", [(r["id"], r["label"], r["valueLabel"]) for r in rows])
missing = sorted(set(map(int, (d.get("settings") or {}).keys())) - {r["id"] for r in rows})
print("settings ids the camera reports but the table lacks:", missing)
EOF
    run curl -m 6 -sS "$B/gopro/media/list"; say ""
    say ""; say "-- webcam stream (1080p, UDP 8554 → us)"
    run curl -m 8 -sS "$B/gopro/webcam/start?res=12&fov=0&port=8554&protocol=TS"; say ""
    if command -v ffprobe >/dev/null; then
        timeout 15 ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate -of default=nw=1 "udp://@:8554" 2>&1 | tee -a "$REPORT"
        timeout 20 ffmpeg -v error -y -i "udp://@:8554" -frames:v 1 "$FRAME" 2>&1 | tee -a "$REPORT"
        [ -s "$FRAME" ] && say "frame saved: $(stat -c %s "$FRAME") bytes"
    fi
    run curl -m 4 -sS "$B/gopro/webcam/stop"; say ""
    run curl -m 4 -sS "$B/gopro/webcam/exit"; say ""
    say ""; say "-- preview stream"
    run curl -m 6 -sS "$B/gopro/camera/stream/start?port=8554"; say ""
    command -v ffprobe >/dev/null && timeout 12 ffprobe -v error -show_entries stream=codec_name,width,height -of default=nw=1 "udp://@:8554" 2>&1 | tee -a "$REPORT"
    run curl -m 4 -sS "$B/gopro/camera/stream/stop"; say ""
}

case "$MODE" in
    usb)
        say ""; say "-- USB network"
        run ip -br addr
        OUT=$(python3 -c 'from Modules.Runtime.actioncam import UsbNetLink; i,ip=UsbNetLink.find(); print(i, ip, UsbNetLink.camera_ip(ip) if ip else "")')
        say "$OUT"
        CAM=$(printf '%s' "$OUT" | awk '{print $3}')
        [ -z "$CAM" ] && { say "no GoPro USB network — camera in GoPro Connect USB mode? (Preferences › Connections › USB Connection)"; exit 2; }
        gopro_probe "$CAM"
        ;;
    uvc)
        say ""; say "-- UVC"
        run ls -l /dev/video*
        command -v v4l2-ctl >/dev/null && run v4l2-ctl --list-devices
        OUT=$(python3 -c "from Modules.Runtime.actioncam import UvcLink; print(*UvcLink.find('${ARG:-insta360}'))")
        say "match '${ARG:-insta360}': $OUT"
        DEV=$(printf '%s' "$OUT" | awk '{print $1}')
        [ -z "$DEV" ] && { say "no matching camera — Webcam mode on the camera, USB data cable?"; exit 2; }
        command -v v4l2-ctl >/dev/null && run v4l2-ctl -d "$DEV" --list-formats-ext
        if command -v ffmpeg >/dev/null; then
            timeout 20 ffmpeg -v error -y -f v4l2 -i "$DEV" -frames:v 1 "$FRAME" 2>&1 | tee -a "$REPORT"
            [ -s "$FRAME" ] && say "frame saved: $(stat -c %s "$FRAME") bytes"
        fi
        ;;
    wifi)
        IFACE="${ARG:-wlan1}"
        say ""; say "-- Wi-Fi on $IFACE (join the camera's network first: Settings › Action cam › Networks)"
        run nmcli -t -f GENERAL.STATE,GENERAL.CONNECTION,IP4.GATEWAY,IP4.ADDRESS dev show "$IFACE"
        if timeout 2 bash -c "echo > /dev/tcp/10.5.5.9/8080" 2>/dev/null; then
            gopro_probe 10.5.5.9
        elif timeout 2 bash -c "echo > /dev/tcp/192.168.42.1/6666" 2>/dev/null; then
            say "Insta360 answers on 192.168.42.1:6666 — sync + keep-alive + capture status:"
            python3 - <<'EOF' 2>&1 | tee -a "$REPORT"
from Modules.Runtime.actioncam import Insta360Wifi, CamError
c = Insta360Wifi()
try:
    c.connect(); print("connected; answers:", c.pump(1.0))
    print("capture status:", c.call("capture_status").hex()[:120])
except CamError as e:
    print("ERR", e)
finally:
    c.close()
EOF
        else
            say "neither 10.5.5.9:8080 (GoPro) nor 192.168.42.1:6666 (Insta360) answers"
        fi
        ;;
    *) echo "usage: $0 usb | uvc [match] | wifi [iface]"; exit 1 ;;
esac
say ""; say "== done — report $REPORT"
