#!/bin/bash
# ============================================================
#  LaLaDash Master Installer  —  Raspberry Pi 4B (4 GB)
#  Run:  bash Installation/install.sh
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_DIR="$SCRIPT_DIR/env"
USER_NAME="$(whoami)"
USER_HOME="$HOME"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

# ── helpers ──────────────────────────────────────────────────
banner()  { echo -e "\n${CYAN}${BOLD}═══  $1  ═══${NC}\n"; }
ok()      { echo -e "${GREEN}[OK]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!!]${NC} $1"; }
fail()    { echo -e "${RED}[ERR]${NC} $1"; }
press()   { echo ""; read -rp "Press Enter to return to menu..."; }

install_available_apt() {
    local available=()
    local missing=()
    local pkg
    for pkg in "$@"; do
        if apt-cache show "$pkg" >/dev/null 2>&1; then
            available+=("$pkg")
        else
            missing+=("$pkg")
        fi
    done
    if [ ${#available[@]} -gt 0 ]; then
        sudo apt install -y "${available[@]}"
    fi
    if [ ${#missing[@]} -gt 0 ]; then
        warn "Packages not available on this distro: ${missing[*]}"
    fi
}

get_boot_config_path() {
    if [ -f "/boot/firmware/config.txt" ]; then
        echo "/boot/firmware/config.txt"
        return 0
    fi
    if [ -f "/boot/config.txt" ]; then
        echo "/boot/config.txt"
        return 0
    fi
    return 1
}

get_boot_cmdline_path() {
    if [ -f "/boot/firmware/cmdline.txt" ]; then
        echo "/boot/firmware/cmdline.txt"
        return 0
    fi
    if [ -f "/boot/cmdline.txt" ]; then
        echo "/boot/cmdline.txt"
        return 0
    fi
    return 1
}

# ── 0  Create / verify virtual-env ──────────────────────────
ensure_venv() {
    banner "Python Virtual Environment"
    if [ -d "$ENV_DIR" ] && [ -f "$ENV_DIR/bin/activate" ]; then
        ok "Venv already exists at $ENV_DIR"
    else
        echo "Creating venv at $ENV_DIR ..."
        python3 -m venv --system-site-packages "$ENV_DIR"
        ok "Venv created"
    fi
    # Always make sure pip is up-to-date inside the venv
    source "$ENV_DIR/bin/activate"
    pip install --upgrade pip setuptools wheel > /dev/null 2>&1
    ok "pip / setuptools / wheel up-to-date"
    press
}

# ── 1  APT packages ─────────────────────────────────────────
install_apt() {
    banner "APT System Packages"

    echo "Updating package lists..."
    sudo apt update

    echo -e "\nInstalling core Python + Qt..."
    sudo apt install -y \
        python3 python3-pip python3-venv \
        python3-pyqt6 python3-pyqt6.qtmultimedia

    echo -e "\nInstalling Qt6/QML runtime modules..."
    install_available_apt \
        python3-pyqt6.qtqml \
        python3-pyqt6.qtquick \
        python3-pyqt6.qtquickwidgets \
        qml6-module-qtqml \
        qml6-module-qtqml-models \
        qml6-module-qtqml-workerscript \
        qml6-module-qtquick \
        qml6-module-qtquick-templates \
        qml6-module-qtquick-controls \
        qml6-module-qtquick-layouts \
        qml6-module-qtquick-window \
        qml6-module-qtquick-effects \
        qml6-module-qt5compat-graphicaleffects \
        qml-module-qtquick-templates2 \
        qml-module-qtqml-workerscript2

    echo -e "\nInstalling audio / media..."
    sudo apt install -y \
        vlc python3-vlc python3-pygame \
        portaudio19-dev libportaudio2 \
        gstreamer1.0-tools gstreamer1.0-libav \
        gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
        gir1.2-gstreamer-1.0 python3-gst-1.0 v4l-utils \
        ffmpeg \
        python3-xlib

    echo -e "\nInstalling OBD-II / CAN bus..."
    sudo apt install -y python3-can can-utils

    echo -e "\nInstalling Bluetooth / D-Bus..."
    sudo apt install -y \
        bluez bluez-tools \
        python3-dbus python3-pydbus python3-gi

    echo -e "\nInstalling misc utilities..."
    sudo apt install -y \
        python3-numpy python3-opencv python3-pil \
        python3-serial python3-requests \
        git libatlas-base-dev libcairo2-dev libgirepository1.0-dev

    echo -e "\nInstalling fonts (Unicode coverage for Qt UI symbols)..."
    sudo apt install -y fonts-noto fonts-noto-extra || \
        sudo apt install -y fonts-noto

    ok "APT packages done"
    press
}

# ── 2  PIP packages (inside venv) ───────────────────────────
install_pip() {
    banner "PIP Packages (venv)"

    if [ ! -f "$ENV_DIR/bin/activate" ]; then
        fail "Venv not found — run option 0 first."
        press; return
    fi

    source "$ENV_DIR/bin/activate"
    echo "Installing from pip_requirements.txt ..."
    pip install -r "$SCRIPT_DIR/pip_requirements.txt"
    pip install --upgrade "typing-extensions>=4.13.1"
    ok "PIP packages done (venv: $ENV_DIR)"
    deactivate
    press
}

# ── 3  GStreamer extras ──────────────────────────────────────
install_gstreamer() {
    banner "GStreamer Extras"

    sudo apt install -y \
        gstreamer1.0-tools gstreamer1.0-libav \
        gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
        gir1.2-gstreamer-1.0 \
        python3-gst-1.0

    ok "GStreamer installed"
    press
}

# ── 4  LaLaDash autostart service ────────────────────────────
setup_laladash_service() {
    banner "LaLaDash Autostart Service"

    # ---- start script ----
    START_SCRIPT="$SCRIPT_DIR/start_laladash.sh"
    cat > "$START_SCRIPT" <<STARTEOF
#!/bin/bash
ERROR_LOG="$USER_HOME/Desktop/LaLaDash_ERROR.txt"
rm -f "\$ERROR_LOG"

export DISPLAY=:0
export XAUTHORITY=$USER_HOME/.Xauthority
export HOME=$USER_HOME
export QT_QPA_PLATFORM=xcb
export XDG_SESSION_TYPE=x11

# Kill on-screen keyboard if running
killall onboard    2>/dev/null
killall squeekboard 2>/dev/null
killall caribou    2>/dev/null
killall matchbox-keyboard 2>/dev/null

# Hide mouse cursor
if command -v unclutter &>/dev/null; then
    pkill unclutter 2>/dev/null
    unclutter -idle 0.1 -root &
fi

cd "$PROJECT_DIR" || {
    echo "ERROR: Could not find LaLaDash folder at \$(date)" >> "\$ERROR_LOG"
    exit 1
}

source "$ENV_DIR/bin/activate" 2>>"\$ERROR_LOG" || {
    echo "ERROR: Failed to activate venv at \$(date)" >> "\$ERROR_LOG"
    exit 1
}

python3 main.py >>"\$ERROR_LOG" 2>&1
if [ \$? -ne 0 ]; then
    echo "ERROR: LaLaDash crashed at \$(date)" >> "\$ERROR_LOG"
fi
exit 0
STARTEOF
    chmod +x "$START_SCRIPT"
    ok "start_laladash.sh written"

    # ---- systemd user service ----
    UNIT_DIR="$USER_HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"

    cat > "$UNIT_DIR/laladash.service" <<SVCEOF
[Unit]
Description=LaLaDash GUI Autostart
After=graphical-session.target

[Service]
Type=simple
Environment=DISPLAY=:0
Environment=XAUTHORITY=$USER_HOME/.Xauthority
ExecStart=$START_SCRIPT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
SVCEOF

    systemctl --user daemon-reload
    systemctl --user enable laladash.service
    ok "laladash.service enabled (user session)"

    echo ""
    echo "  LaLaDash will auto-start on next login."
    echo "  Manual control:"
    echo "    systemctl --user start  laladash"
    echo "    systemctl --user stop   laladash"
    echo "    systemctl --user status laladash"
    press
}

# ── 5  MPRIS Bluetooth proxy service ────────────────────────
setup_mpris_service() {
    banner "MPRIS Bluetooth Proxy Service"

    # Ensure the bluez package (which ships mpris-proxy) is present
    if ! command -v mpris-proxy >/dev/null 2>&1; then
        echo "mpris-proxy not found — installing bluez..."
        sudo apt install -y bluez
    fi

    if ! command -v mpris-proxy >/dev/null 2>&1; then
        fail "mpris-proxy binary still not found after apt install. Bluetooth AVRCP bridging will NOT work."
        press; return
    fi

    UNIT_DIR="$USER_HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"

    cat > "$UNIT_DIR/mpris-proxy.service" <<MPEOF
[Unit]
Description=Bluetooth MPRIS Proxy (LaLaDash)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
Environment=XDG_RUNTIME_DIR=%t
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
ExecStart=/usr/bin/mpris-proxy
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
MPEOF

    systemctl --user daemon-reload
    systemctl --user enable --now mpris-proxy.service
    ok "mpris-proxy.service enabled and started (user session)"

    echo ""
    echo "  Status:"
    systemctl --user status mpris-proxy.service --no-pager -l 2>/dev/null || true
    echo ""
    echo "  Manual control:"
    echo "    systemctl --user start  mpris-proxy"
    echo "    systemctl --user stop   mpris-proxy"
    echo "    systemctl --user status mpris-proxy"
    echo ""
    echo "  Verify AVRCP bridge:"
    echo "    busctl --user list | grep MediaPlayer2"
    press
}

# ── 6  Bluetooth setup helper ───────────────────────────────
setup_bluetooth() {
    banner "Bluetooth Configuration"

    sudo systemctl enable bluetooth
    sudo systemctl start  bluetooth

    echo "Setting Bluetooth discoverable and pairable..."
    sudo bluetoothctl <<BTEOF
power on
discoverable on
pairable on
agent on
default-agent
BTEOF

    ok "Bluetooth is up and discoverable"
    echo "  Use 'bluetoothctl' to pair devices."
    press
}

# ── 7  Desktop shortcut (manual launch) ──────────────────────
create_desktop_shortcut() {
    banner "Desktop Shortcut"

    DESKTOP_DIR="$USER_HOME/Desktop"
    mkdir -p "$DESKTOP_DIR"

    SHORTCUT="$DESKTOP_DIR/LaLaDash.desktop"
    ICON_PATH="$PROJECT_DIR/GUI/Icons/dashboard.png"

    # ---- launcher script (no terminal, hide keyboard, hide cursor) ----
    LAUNCHER="$SCRIPT_DIR/launch_laladash.sh"
    cat > "$LAUNCHER" <<LAUNCHEOF
#!/bin/bash
# ── LaLaDash manual launcher ──
# Hides on-screen keyboard, hides cursor, launches fullscreen

ERROR_LOG="$USER_HOME/Desktop/LaLaDash_ERROR.txt"
rm -f "\$ERROR_LOG"

export DISPLAY=:0
export XAUTHORITY=$USER_HOME/.Xauthority
export HOME=$USER_HOME
export QT_QPA_PLATFORM=xcb
export XDG_SESSION_TYPE=x11

# Kill on-screen keyboard if running
killall onboard    2>/dev/null
killall squeekboard 2>/dev/null
killall caribou    2>/dev/null
killall matchbox-keyboard 2>/dev/null

# Hide mouse cursor (unclutter must be installed)
if command -v unclutter &>/dev/null; then
    pkill unclutter 2>/dev/null
    unclutter -idle 0.1 -root &
fi

cd "$PROJECT_DIR" || {
    echo "ERROR: Could not find LaLaDash folder at \$(date)" >> "\$ERROR_LOG"
    exit 1
}

source "$ENV_DIR/bin/activate" 2>>"\$ERROR_LOG" || {
    echo "ERROR: Failed to activate venv at \$(date)" >> "\$ERROR_LOG"
    exit 1
}

python3 main.py >>"\$ERROR_LOG" 2>&1
EXIT_CODE=\$?

# Restore cursor when app exits
if command -v unclutter &>/dev/null; then
    pkill unclutter 2>/dev/null
fi

if [ \$EXIT_CODE -ne 0 ]; then
    echo "ERROR: LaLaDash crashed at \$(date)" >> "\$ERROR_LOG"
fi
exit 0
LAUNCHEOF

    chmod +x "$LAUNCHER"

    # ---- .desktop file ----
    cat > "$SHORTCUT" <<DSKEOF
[Desktop Entry]
Name=LaLaDash
Comment=Car dashboard
Exec=$LAUNCHER
Icon=$ICON_PATH
Terminal=false
Type=Application
Categories=Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=false
DSKEOF

    chmod +x "$SHORTCUT"
    # Trust the desktop file (GNOME/Pop!_OS)
    gio set "$SHORTCUT" metadata::trusted true 2>/dev/null

    # Install unclutter for cursor hiding
    sudo apt install -y unclutter 2>/dev/null || warn "unclutter not available — cursor won't auto-hide"

    ok "Desktop shortcut created: $SHORTCUT"
    echo ""
    echo "  Double-click 'LaLaDash' on the Desktop to launch."
    echo "  - No terminal window"
    echo "  - On-screen keyboard killed"
    echo "  - Mouse cursor hidden while running"
    echo "  - App runs fullscreen (via settings.ini)"
    press
}

# ── 10  Force 1280x800 display mode (Raspberry Pi) ─────────
configure_display_1280x800() {
    banner "Force Display Mode: 1280x800"

    # ── Part A: xrandr (takes effect immediately, no reboot) ──
    echo "Trying xrandr to switch NOW..."
    XRANDR_OUTPUT=""
    if command -v xrandr >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
        XRANDR_OUTPUT="$(xrandr 2>/dev/null | awk '/ connected /{print $1; exit}')"
        if [ -n "$XRANDR_OUTPUT" ]; then
            if xrandr --output "$XRANDR_OUTPUT" --mode 1280x800 2>/dev/null; then
                ok "xrandr: switched $XRANDR_OUTPUT to 1280x800 (active immediately)"
            else
                warn "xrandr: 1280x800 mode not in EDID — adding custom CVT mode..."
                CVTLINE="$(cvt 1280 800 60 2>/dev/null | grep Modeline | sed 's/Modeline //')"
                if [ -n "$CVTLINE" ]; then
                    MODENAME="$(echo "$CVTLINE" | awk '{print $1}' | tr -d '"')"
                    xrandr --newmode $CVTLINE 2>/dev/null || true
                    xrandr --addmode "$XRANDR_OUTPUT" "$MODENAME" 2>/dev/null || true
                    xrandr --output "$XRANDR_OUTPUT" --mode "$MODENAME" 2>/dev/null \
                        && ok "xrandr: custom CVT mode applied" \
                        || warn "xrandr: could not apply mode — display may not support 1280x800"
                fi
            fi
        else
            warn "xrandr: no connected output found (is DISPLAY set?)"
        fi
    else
        warn "xrandr not available or DISPLAY not set — skipping runtime switch"
    fi

    # ── Part B: cmdline.txt (survives reboot via kernel video= param) ──
    local cmdline
    if ! cmdline="$(get_boot_cmdline_path)"; then
        warn "Could not find Raspberry Pi cmdline.txt — skipping boot-time fix."
    else
        local video_params="video=HDMI-A-1:1280x800M@60 video=HDMI-A-2:1280x800M@60"
        local current
        current="$(cat "$cmdline")"
        if echo "$current" | grep -q "video=HDMI-A-1:1280x800M@60"; then
            ok "cmdline.txt: video= params already present, skipping."
        else
            # Remove any stale video= entries we may have written before
            local cleaned
            cleaned="$(echo "$current" | sed 's/ video=HDMI-A-[^ ]*//g')"
            echo "${cleaned} ${video_params}" | sudo tee "$cmdline" >/dev/null
            ok "cmdline.txt: added ${video_params} (takes effect after reboot)"
        fi
    fi

    echo ""
    echo "  Summary:"
    echo "  - xrandr switch: immediate (if output supports 1280x800)"
    echo "  - cmdline.txt:   video=HDMI-A-1:1280x800M@60 video=HDMI-A-2:1280x800M@60 (survives reboot)"
    press
}

# ── 8  Install ALL (full setup) ─────────────────────────────
install_all() {
    banner "Full Installation"
    echo "This will run: venv -> apt -> pip -> gstreamer -> services -> desktop shortcut -> bluetooth -> rpiplay"
    read -rp "Continue? [y/N] " yn
    case "$yn" in
        [Yy]*)
            ensure_venv
            install_apt
            install_pip
            install_gstreamer
            setup_laladash_service
            setup_mpris_service
            create_desktop_shortcut
            setup_bluetooth
            configure_display_1280x800
            install_rpiplay
            banner "Full Installation Complete"
            ok "Everything is installed and configured."
            echo "  Reboot recommended — select option 9 from the menu."
            press
            ;;
        *) echo "Cancelled."; press ;;
    esac
}

# ── 9  Reboot ───────────────────────────────────────────────
do_reboot() {
    banner "Reboot"
    read -rp "Reboot now? [y/N] " yn
    case "$yn" in
        [Yy]*) echo "Rebooting..."; sudo reboot ;;
        *)     echo "Cancelled."; press ;;
    esac
}

# ── MENU ─────────────────────────────────────────────────────
# ── 11  RPiPlay (AirPlay receiver, build from source) ───────
# Does the rpiplay on PATH actually have a video renderer that draws?
# RPiPlay always compiles the `dummy` renderer in; the real ones are added
# only if their dev packages were present at configure time, and CMake does
# not complain when they were not. A dummy-only binary accepts the iPhone's
# stream and discards every frame, so the phone reports "mirroring" while the
# screen stays black — which is exactly what "AirPlay does not work" looks
# like. "Is it installed?" is therefore the wrong question to ask.
rpiplay_has_real_renderer() {
    command -v rpiplay >/dev/null 2>&1 || return 1
    rpiplay -h 2>&1 \
        | sed -n '/-vr renderer/,/-ar renderer/p' \
        | grep -qE '^[[:space:]]+(gstreamer|rpi):'
}

install_rpiplay() {
    banner "RPiPlay — AirPlay Receiver"

    if rpiplay_has_real_renderer; then
        ok "rpiplay already installed at $(which rpiplay) with a real renderer"
        rpiplay -h 2>&1 | sed -n '/-vr renderer/,/-ar renderer/p'
        press; return
    fi

    if command -v rpiplay >/dev/null 2>&1; then
        warn "rpiplay at $(which rpiplay) has only the dummy renderer — mirroring"
        warn "would connect and show a black screen. Rebuilding from source."
    fi

    echo "Installing build dependencies..."
    # The -dev packages are what the build needs to compile the GStreamer
    # renderer in; the plugin packages are what it needs at run time. Missing
    # either produces a receiver that looks installed and shows nothing.
    sudo apt install -y \
        cmake \
        libavahi-compat-libdnssd-dev \
        libplist-dev \
        libssl-dev \
        libgstreamer1.0-dev \
        libgstreamer-plugins-base1.0-dev \
        libgstreamer-plugins-bad1.0-dev \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-libav \
        gstreamer1.0-gl

    local build_dir
    build_dir="$(mktemp -d /tmp/rpiplay_build_XXXXX)"
    echo "Cloning RPiPlay..."
    git clone --depth=1 https://github.com/FD-/RPiPlay.git "$build_dir"

    echo "Building RPiPlay (takes a few minutes on RPi4)..."
    cmake -S "$build_dir" -B "$build_dir/build" -DCMAKE_BUILD_TYPE=Release
    cmake --build "$build_dir/build" --parallel "$(nproc)"
    sudo cmake --install "$build_dir/build"
    sudo ldconfig

    rm -rf "$build_dir"

    if rpiplay_has_real_renderer; then
        ok "rpiplay installed: $(which rpiplay)"
        rpiplay -h 2>&1 | sed -n '/-vr renderer/,/-ar renderer/p'
    elif command -v rpiplay >/dev/null 2>&1; then
        warn "rpiplay built, but STILL only has the dummy renderer."
        warn "The GStreamer dev packages were not visible to cmake. Check the"
        warn "configure output above for 'gstreamer' and rebuild."
    else
        warn "rpiplay binary not found after build — check output above"
    fi
    press
}

show_menu() {
    clear
    echo -e "${BOLD}${CYAN}"
    echo "  ╔═══════════════════════════════════════════╗"
    echo "  ║        LaLaDash Master Installer          ║"
    echo "  ║        Raspberry Pi 4B  (4 GB)            ║"
    echo "  ╠═══════════════════════════════════════════╣"
    echo "  ║                                           ║"
    echo "  ║   0)  Create Python venv                  ║"
    echo "  ║   ─────────────────────────────────       ║"
    echo "  ║   1)  Install APT packages                ║"
    echo "  ║   2)  Install PIP packages (venv)         ║"
    echo "  ║   3)  Install GStreamer extras             ║"
    echo "  ║   ─────────────────────────────────       ║"
    echo "  ║   4)  Setup LaLaDash autostart service    ║"
    echo "  ║   5)  Setup MPRIS Bluetooth proxy         ║"
    echo "  ║   6)  Setup Bluetooth                     ║"
    echo "  ║   7)  Create Desktop shortcut             ║"
    echo "  ║   ─────────────────────────────────       ║"
    echo "  ║   8)  Install EVERYTHING                  ║"
    echo "  ║   ─────────────────────────────────       ║"
    echo "  ║   9)  Reboot                              ║"
    echo "  ║  10)  Force display 1280x800              ║"
    echo "  ║  11)  Install RPiPlay (AirPlay)           ║"
    echo "  ║   Q)  Exit                                ║"
    echo "  ║                                           ║"
    echo "  ╚═══════════════════════════════════════════╝"
    echo -e "${NC}"
    echo -n "  Select: "
}

# ── MAIN ─────────────────────────────────────────────────────
while true; do
    show_menu
    read -r choice
    case "$choice" in
        0) ensure_venv ;;
        1) install_apt ;;
        2) install_pip ;;
        3) install_gstreamer ;;
        4) setup_laladash_service ;;
        5) setup_mpris_service ;;
        6) setup_bluetooth ;;
        7) create_desktop_shortcut ;;
        8) install_all ;;
        9) do_reboot ;;
        10) configure_display_1280x800 ;;
        11) install_rpiplay ;;
        q|Q) echo "Bye!"; exit 0 ;;
        *) warn "Invalid option"; sleep 1 ;;
    esac
done
