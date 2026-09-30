#!/bin/bash
# LaLaDash QML launcher — GPU-accelerated via eglfs (no X11)

ERROR_LOG="$HOME/Desktop/LaLaDash_ERROR.txt"

# Remove old error file on each boot
rm -f "$ERROR_LOG"

# ── Platform: eglfs (direct GPU rendering, no X server) ──────────────────
export QT_QPA_PLATFORM=eglfs
export QT_OPENGL=gles2

# eglfs configuration (create /etc/qt6/eglfs.json if needed)
# export QT_QPA_EGLFS_KMS_CONFIG=/etc/qt6/eglfs.json

# ── Fallback: uncomment these two lines to use X11 instead ───────────────
# export QT_QPA_PLATFORM=xcb
# export DISPLAY=:0

# ── D-Bus session bus ────────────────────────────────────────────────────
# Headless eglfs has no desktop session, so pin the per-user bus explicitly.
# Without this, LaLaDash's MPRIS player lands on an auto-launched private bus
# that mpris-proxy can't see — and the car head unit never detects it.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

# ── QML scene graph debug (uncomment to see frame timings) ───────────────
# export QSG_RENDER_TIMING=1

# Move to LaLaDash folder
cd "$(dirname "$0")/.." || {
    echo "ERROR: Could not find LaLaDash folder at $(date)" >> "$ERROR_LOG"
    exit 1
}

# Activate virtual environment
source Installation/env/bin/activate 2>>"$ERROR_LOG" || {
    echo "ERROR: Failed to activate Python venv at $(date)" >> "$ERROR_LOG"
    exit 1
}

# Run LaLaDash QML
python3 main_qml.py >>"$ERROR_LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "ERROR: LaLaDash crashed at $(date)" >> "$ERROR_LOG"
fi

exit 0
