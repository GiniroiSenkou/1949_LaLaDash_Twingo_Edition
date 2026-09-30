#!/bin/bash
# Trim the Pi's session down to what a car dashboard actually needs.
#
# Run on the Pi:  Installation/pi-config/apply_boot_config.sh
#                 Installation/pi-config/apply_boot_config.sh --revert
#
# Idempotent. Everything it switches off is either invisible on a kiosk screen
# or actively in the way; nothing here touches avahi (AirPlay discovery),
# bluetooth (the head unit's AVRCP link), rtkit (audio thread priority),
# NetworkManager, or lightdm.

set -u

LABWC_SYS="/etc/xdg/labwc/autostart"
LABWC_BACKUP="/etc/xdg/labwc/autostart.laladash-orig"
XDG_AUTOSTART="$HOME/.config/autostart"

# Desktop-session extras that a dashboard never shows. Each is an entry in
# /etc/xdg/autostart shadowed by a Hidden=true file of the same name here.
HIDE_ENTRIES=(
    squeekboard                        # on-screen keyboard
    xcompmgr                           # raspi-config's compositor starter
    lxpolkit                           # auth prompt agent
    polkit-mate-authentication-agent-1 # second auth prompt agent
    gnome-keyring-pkcs11
    gnome-keyring-secrets
    gnome-keyring-ssh
    pprompt                            # Pi first-run prompt
)

# System services with no role in a car.
DISABLE_SERVICES=(
    cups.service          # printing
    cups-browsed.service  # printer discovery
    ModemManager.service  # cellular modems
    triggerhappy.service  # global hotkey daemon
)

# Per-user volume monitors for hardware this machine never sees. The udisks2
# one is left alone so a USB stick still mounts.
MASK_USER_UNITS=(
    gvfs-afc-volume-monitor.service      # Apple File Conduit (USB iOS, not AirPlay)
    gvfs-goa-volume-monitor.service      # GNOME Online Accounts
    gvfs-gphoto2-volume-monitor.service  # cameras
    gvfs-mtp-volume-monitor.service      # MTP phones
)

revert() {
    echo "== reverting =="
    # An earlier version of this script dropped a user-level autostart, which
    # was wrong: labwc runs with -m (--merge-config), so user and system files
    # are merged rather than overriding, and it only ever added duplicates.
    rm -f "$HOME/.config/labwc/autostart"
    if [ -f "$LABWC_BACKUP" ]; then
        sudo cp "$LABWC_BACKUP" "$LABWC_SYS"
        echo "  restored $LABWC_SYS from backup"
    fi
    for e in "${HIDE_ENTRIES[@]}"; do
        rm -f "$XDG_AUTOSTART/$e.desktop"
    done
    echo "  re-enabled ${#HIDE_ENTRIES[@]} autostart entries"
    for s in "${DISABLE_SERVICES[@]}"; do
        sudo systemctl enable --now "$s" >/dev/null 2>&1
    done
    echo "  re-enabled ${#DISABLE_SERVICES[@]} system services"
    for u in "${MASK_USER_UNITS[@]}"; do
        systemctl --user unmask "$u" >/dev/null 2>&1
    done
    echo "  unmasked ${#MASK_USER_UNITS[@]} user units"
    echo "Reboot to restore the full desktop session."
    exit 0
}

[ "${1:-}" = "--revert" ] && revert

echo "== labwc autostart =="
# labwc is launched as `labwc -m` by /usr/bin/labwc-pi, and -m merges the user
# config with the system one instead of replacing it. A file dropped in
# ~/.config/labwc therefore cannot switch anything off -- the system file's
# lines still run -- so the two desktop pieces are commented out in place.
# Commenting rather than deleting keeps the file readable as a diff against
# the stock one, and re-running is a no-op because the pattern is anchored.
rm -f "$HOME/.config/labwc/autostart"
if [ -f "$LABWC_SYS" ]; then
    sudo cp -n "$LABWC_SYS" "$LABWC_BACKUP"
    # pcmanfm --desktop draws the wallpaper and desktop icons: this *is* the
    # desktop seen before the dashboard appears. wf-panel-pi is the taskbar,
    # and it owned the top 36 px, which held the app window to 1280x764.
    sudo sed -i -E \
        's|^([[:space:]]*[^#[:space:]].*pcmanfm[[:space:]]+--desktop.*)$|# LaLaDash: desktop off -- \1|' \
        "$LABWC_SYS"
    sudo sed -i -E \
        's|^([[:space:]]*[^#[:space:]].*wf-panel-pi.*)$|# LaLaDash: panel off -- \1|' \
        "$LABWC_SYS"
    echo "  patched $LABWC_SYS (backup at $LABWC_BACKUP)"
    echo "  active lines now:"
    grep -vE '^[[:space:]]*(#|$)' "$LABWC_SYS" | sed 's/^/    /'
else
    echo "  WARNING: $LABWC_SYS not found" >&2
fi

echo "== desktop autostart entries =="
mkdir -p "$XDG_AUTOSTART"
for e in "${HIDE_ENTRIES[@]}"; do
    if [ -f "/etc/xdg/autostart/$e.desktop" ]; then
        cat > "$XDG_AUTOSTART/$e.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$e
Exec=/bin/true
Hidden=true
X-GNOME-Autostart-enabled=false
EOF
        echo "  disabled $e"
    fi
done

echo "== system services =="
for s in "${DISABLE_SERVICES[@]}"; do
    if systemctl cat "$s" >/dev/null 2>&1; then
        sudo systemctl disable --now "$s" >/dev/null 2>&1
        echo "  disabled $s"
    fi
done

echo "== per-user units =="
for u in "${MASK_USER_UNITS[@]}"; do
    systemctl --user mask "$u" >/dev/null 2>&1 && echo "  masked $u"
done

echo
echo "Done. The desktop shell is gone from the next boot; LaLaDash covers the"
echo "screen itself. Re-run with --revert to put the desktop back."
