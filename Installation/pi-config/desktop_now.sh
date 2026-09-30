#!/bin/bash
# Bring the Raspberry Pi desktop up in place of LaLaDash, right now.
#
# The car boots straight into the app with no desktop shell behind it
# (apply_boot_config.sh commented pcmanfm/wf-panel-pi out of labwc's
# autostart), so quitting the app used to leave a black screen. This runs the
# two lines that script commented out — verbatim, from the file — stops the
# app's unit, and drops a "LaLaDash" icon on the desktop that brings the
# dashboard back (dashboard_now.sh). Nothing here survives a reboot: the next
# boot is the dashboard again.
#
# Started by Settings → About → "Close → Desktop" through `systemd-run --user`,
# i.e. in its own transient unit, because anything started from inside the
# app's cgroup dies with the app when its unit stops.
set -u
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"
LABWC_SYS=/etc/xdg/labwc/autostart

mkdir -p "$HOME/Desktop"
cat > "$HOME/Desktop/LaLaDash.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=LaLaDash
Comment=Back to the dashboard
Exec=$HERE/dashboard_now.sh
Icon=$HERE/../../GUI/Icons/default_car.png
Terminal=false
DESK
chmod +x "$HOME/Desktop/LaLaDash.desktop" 2>/dev/null

systemctl --user stop laladash.service

# The desktop pieces exactly as the stock session starts them.
mapfile -t CMDS < <(sed -nE 's/^# LaLaDash: (desktop|panel) off -- //p' "$LABWC_SYS" 2>/dev/null)
if [ "${#CMDS[@]}" -eq 0 ]; then
    CMDS=("pcmanfm --desktop --profile LXDE-pi" "wf-panel-pi")
fi
for c in "${CMDS[@]}"; do
    name="${c%% *}"
    pgrep -x "$(basename "$name")" >/dev/null 2>&1 && continue
    bash -c "$c" >/dev/null 2>&1 &
done
wait
