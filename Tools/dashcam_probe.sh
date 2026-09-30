#!/bin/bash
# 70mai dashcam probe — run ON THE PI, ignition on (camera powered), engine off.
#
#   Tools/dashcam_probe.sh [iface] [--ssid NAME] [--password PW] [--save] [--no-pair]
#
# Joins the camera's hotspot on <iface> (default wlan0 — the built-in radio;
# keep your SSH session on the USB adapter, this one leaves the house Wi-Fi
# for a few minutes), then records everything the app needs to know about
# this particular model: address, open ports, whether unsigned calls work,
# pairing (you press the camera's power button once when told), every status
# command, the RTSP path that answers, one JPEG frame.
#
# Report: ~/dashcam_probe.txt (+ ~/dashcam_frame.jpg). With --save the
# token / user id / signing variant / rtsp path go straight into
# Data/settings.ini [Dashcam] so the app never has to pair again.
#
# Everything here goes through Modules/Runtime/dashcam.py — the same code the
# app runs — so a pass here is a pass for the 70mai page.
set -u
cd "$(dirname "$0")/.." || exit 1
IFACE=wlan0; SSID=""; PW=12345678; SAVE=0; PAIR=1
while [ $# -gt 0 ]; do
    case "$1" in
        --ssid) SSID="$2"; shift 2 ;;
        --password) PW="$2"; shift 2 ;;
        --save) SAVE=1; shift ;;
        --no-pair) PAIR=0; shift ;;
        -*) echo "unknown option $1"; exit 1 ;;
        *) IFACE="$1"; shift ;;
    esac
done
REPORT="$HOME/dashcam_probe.txt"
FRAME="$HOME/dashcam_frame.jpg"
DC="python3 -m Modules.Runtime.dashcam"
: > "$REPORT"
say() { printf '%s\n' "$*" | tee -a "$REPORT"; }
run() { say "\$ $*"; "$@" 2>&1 | tee -a "$REPORT"; }
t0=$(date +%s)
say "== 70mai probe $(date '+%F %T') on $(hostname) iface=$IFACE"

# 1 — what is in range
say ""; say "-- networks starting with 70mai on $IFACE"
run $DC detect --iface "$IFACE"

# 2 — join (reuses a profile the Wi-Fi manager saved; else creates ours)
say ""; say "-- join"
JOIN_ARGS=(--iface "$IFACE" --password "$PW")
[ -n "$SSID" ] && JOIN_ARGS+=(--ssid "$SSID")
if ! run $DC join "${JOIN_ARGS[@]}"; then
    say "could not join — stopping here"; exit 2
fi
STATE=$($DC state --iface "$IFACE")
IP=$(printf '%s' "$STATE" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("gateway",""))')
MYIP=$(printf '%s' "$STATE" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("address",""))')
say "camera at $IP, we are $MYIP"
[ -z "$IP" ] && { say "no gateway — stopping"; exit 2; }
URL="http://$IP"

# 3 — ports
say ""; say "-- ports"
for port in 21 23 80 554 8080 1935; do
    if timeout 2 bash -c "echo > /dev/tcp/$IP/$port" 2>/dev/null; then say "  $port open"; else say "  $port closed"; fi
done

# 4 — unsigned calls (the CVE'd firmwares answer these without a token)
say ""; say "-- unsigned"
for c in getdeviceattr getsdstate getwifi; do
    run $DC call --url "$URL" "$c"
done
say '$ curl -m 3 -sS http://'"$IP"'/'
curl -m 3 -sS "$URL/" 2>&1 | head -c 400 | tee -a "$REPORT"; say ""

# 5 — pairing (one button press on the camera)
TOKEN=""; USERID=""; VARIANT="?"
if [ $PAIR -eq 1 ]; then
    say ""; say "-- pairing: when the line below says so, PRESS THE CAMERA'S POWER BUTTON"
    PAIR_OUT=$($DC pair --url "$URL" 2>&1 | tee -a "$REPORT")
    TOKEN=$(printf '%s\n' "$PAIR_OUT" | awk '/^token /{print $2}')
    USERID=$(printf '%s\n' "$PAIR_OUT" | awk '/^user id /{print $3}')
    VARIANT=$(printf '%s\n' "$PAIR_OUT" | sed -n "s/^variant ok '\(.*\)'$/\1/p")
    [ -z "$VARIANT" ] && VARIANT="?"
    if [ -n "$TOKEN" ]; then
        say "token ok (${#TOKEN} chars), variant '$VARIANT'"
        say ""; say "-- signed"
        for c in getdeviceattr getsdstate getfilecount getgpsstatus getwifi getsystime getAllMenu; do
            run $DC call --url "$URL" --token "$TOKEN" --variant "$VARIANT" "$c"
        done
        run $DC call --url "$URL" --token "$TOKEN" --variant "$VARIANT" getfilelist start=1 end=3 type=1
        run $DC call --url "$URL" --token "$TOKEN" --variant "$VARIANT" getfilelist start=1 end=3 type=0
        run $DC call --url "$URL" --token "$TOKEN" --variant "$VARIANT" client operation=register ip="$MYIP"
    else
        say "pairing did not produce a token"
    fi
fi

# 6 — RTSP
say ""; say "-- rtsp"
RTSP_OUT=$($DC rtsp "$IP" 2>&1 | tee -a "$REPORT")
RTSP=$(printf '%s\n' "$RTSP_OUT" | awk '/^OK/{print $2; exit}')
if [ -n "$RTSP" ] && command -v ffprobe >/dev/null; then
    say '$ ffprobe '"$RTSP"
    timeout 15 ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name,width,height,r_frame_rate \
        -of default=nw=1 "$RTSP" 2>&1 | tee -a "$REPORT"
    say '$ ffmpeg one frame → '"$FRAME"
    timeout 20 ffmpeg -v error -y -rtsp_transport tcp -i "$RTSP" -frames:v 1 "$FRAME" 2>&1 | tee -a "$REPORT"
    [ -s "$FRAME" ] && say "frame saved: $(stat -c %s "$FRAME") bytes"
fi

# 7 — hotspot stamina: is the SSID still up after we leave it alone a while?
say ""; say "-- link after 60 s idle"
sleep 60
run $DC state --iface "$IFACE"

# 8 — hand back what the app needs
if [ $SAVE -eq 1 ]; then
    say ""; say "-- saving into Data/settings.ini"
    ARGS=()
    [ -n "$TOKEN" ] && ARGS+=("token=$TOKEN" "user_id=$USERID" "sign_variant=$VARIANT")
    [ -n "$RTSP" ] && ARGS+=("rtsp_path=${RTSP#rtsp://$IP:554}")
    [ -n "$SSID" ] && ARGS+=("ssid=$SSID")
    ARGS+=("iface=$IFACE" "password=$PW")
    run $DC save "${ARGS[@]}"
fi

say ""; say "== done in $(( $(date +%s) - t0 )) s — report $REPORT"
say "   (the camera link stays up; '$DC leave --iface $IFACE' drops it)"
