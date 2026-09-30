#!/bin/bash
# LaLaDash launcher — what the systemd user unit actually runs on the Pi.
#
# Paths are derived from this script's own location rather than hardcoded, so
# the same file works from the dev checkout and from ~/Desktop/LaLaDash on the
# car. The previous version pinned a fixed home directory and a venv name that has not
# existed for months, which made it useless as the repo's copy.

APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
ERROR_LOG="$(dirname "$APP_DIR")/LaLaDash_ERROR.txt"

rm -f "$ERROR_LOG"

export DISPLAY=:0
export XAUTHORITY="$HOME/.Xauthority"
export QT_QPA_PLATFORM=xcb
export XDG_SESSION_TYPE=x11

# At boot this unit can win the race against the session's own autostart, which
# is what creates the Xauthority and brings Xwayland up. Starting Qt before
# then dies with "could not connect to display :0". Waits for the display to
# actually answer, not merely for the socket to exist.
for _ in $(seq 1 300); do
    xset q >/dev/null 2>&1 && break
    sleep 0.1
done

cd "$APP_DIR" || {
    echo "ERROR: Could not find LaLaDash folder at $(date)" >> "$ERROR_LOG"
    exit 1
}

source "$APP_DIR/Installation/env/bin/activate" 2>>"$ERROR_LOG" || {
    echo "ERROR: Failed to activate venv at $(date)" >> "$ERROR_LOG"
    exit 1
}

# Cover the desktop straight away. main.py needs ~8 s to reach a mapped window;
# the splash is on screen in well under one and exits by itself the moment the
# real window appears, so the driver never sees the desktop underneath.
SPLASH_PID=""
if [ -f "$APP_DIR/Installation/splash.py" ]; then
    python3 "$APP_DIR/Installation/splash.py" >>"$ERROR_LOG" 2>&1 &
    SPLASH_PID=$!
fi

# On-screen keyboards steal focus from a kiosk app that never needs one.
killall onboard           2>/dev/null
killall squeekboard       2>/dev/null
killall caribou           2>/dev/null
killall matchbox-keyboard 2>/dev/null

# Hide the mouse cursor — this is a touch panel.
if command -v unclutter &>/dev/null; then
    pkill unclutter 2>/dev/null
    unclutter -idle 0.1 -root &
fi

python3 main.py >>"$ERROR_LOG" 2>&1
RC=$?

# Backstop: the splash normally exits on its own once the window is up, but a
# start-up that dies before mapping anything would otherwise leave it running.
if [ -n "$SPLASH_PID" ]; then
    kill "$SPLASH_PID" 2>/dev/null
fi

if [ $RC -ne 0 ]; then
    echo "ERROR: LaLaDash crashed at $(date)" >> "$ERROR_LOG"
fi

exit 0
