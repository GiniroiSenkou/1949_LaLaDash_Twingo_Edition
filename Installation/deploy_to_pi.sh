#!/bin/bash
# Push this checkout to the car. The dev PC is the source of truth; the Pi is
# a deployment target and never edited directly.
#
#   Installation/deploy_to_pi.sh              code + assets, restart, tail log
#   Installation/deploy_to_pi.sh --with-state also push Data/ runtime state
#   Installation/deploy_to_pi.sh --dry-run    list what would change
#   Installation/deploy_to_pi.sh --no-restart leave the service alone
#   Installation/deploy_to_pi.sh --exclude=P   skip an extra rsync pattern (repeatable)
#
# Installation/transfer.sh is the older interactive menu and cannot be
# scripted; this is the non-interactive path.

set -u

# Machine-specific values (Pi address, key) live in Installation/local.env,
# which is gitignored. See local.env.example.
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/local.env" ] && . "$HERE/local.env"

PI_KEY="${PI_KEY:-$HOME/.ssh/id_ed25519}"
PI_USER="${PI_USER:-pi}"
PI_PATH="${PI_PATH:-/home/$PI_USER/Desktop/LaLaDash}"

# The Pi's address can depend on which Wi-Fi adapter carried it up. PI_HOST
# pins one explicitly; otherwise the candidates (PI_CANDIDATES, a
# space-separated list, default raspberrypi.local) are tried in order and the
# first that answers SSH wins.
read -r -a PI_CANDIDATES <<< "${PI_CANDIDATES:-raspberrypi.local}"

resolve_pi_host() {
    if [ -n "${PI_HOST:-}" ]; then
        echo "$PI_HOST"
        return 0
    fi
    for h in "${PI_CANDIDATES[@]}"; do
        if ssh -i "$PI_KEY" -o ConnectTimeout=4 -o BatchMode=yes \
               -o StrictHostKeyChecking=accept-new "$PI_USER@$h" true 2>/dev/null; then
            echo "$PI_USER@$h"
            return 0
        fi
    done
    return 1
}

if ! PI_HOST="$(resolve_pi_host)"; then
    echo "Pi not reachable at any of: ${PI_CANDIDATES[*]} (set PI_HOST=user@<ip> to pin one)" >&2
    exit 1
fi
echo "== Pi answers at $PI_HOST =="

APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"

WITH_STATE=0
DRY_RUN=0
RESTART=1
EXTRA_EXCLUDES=()
for arg in "$@"; do
    case "$arg" in
        --with-state) WITH_STATE=1 ;;
        --dry-run)    DRY_RUN=1 ;;
        --no-restart) RESTART=0 ;;
        --exclude=*)  EXTRA_EXCLUDES+=("--exclude=${arg#--exclude=}") ;;
        -h|--help)    sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

# Never shipped:
#   Cars/                 1.5 GB of engine samples that live only on the Pi.
#   Installation/env/     the Pi's venv; the dev PC's interpreter is not it.
#   Installation/libshim/ generated at runtime from *local* library paths, so a
#                         dev-PC run seeds an x86_64 symlink that dangles on ARM.
EXCLUDES=(
    --exclude='/Cars/'
    --exclude='/Installation/env/'
    --exclude='/Installation/libshim/'
    --exclude='__pycache__/'
    --exclude='*.pyc'
    --exclude='.git/'
    --exclude='.venv/'
    --exclude='.idea/'
    --exclude='.vscode/'
    --exclude='.continue/'
    --exclude='.claude/'
    # Caches are derived from the device's own library and are rebuilt on
    # demand. Never shipped, not even with --with-state: pushing the dev PC's
    # would delete the art the car has already fetched and force a re-fetch of
    # the lot.
    --exclude='/Data/.cache/'
    --exclude='/Data/*_cache/'
)

# Runtime state belongs to whichever machine produced it. Pushing the dev PC's
# settings.ini is what put `fullscreen = false` and dev-PC sound toggles on the
# car; music.json indexes that machine's own library scan. --with-state is the
# deliberate override for when a settings change really is the thing you want
# to deploy.
if [ "$WITH_STATE" -eq 0 ]; then
    EXCLUDES+=(
        --exclude='/Data/settings.ini'
        --exclude='/Data/settings.ini.*'
        --exclude='/Data/music.json'
        --exclude='/Data/music_backup.json'
        --exclude='/Data/music_session.json'
        --exclude='/Data/*.log'
        # The CAN signal map is edited on the car by the debugger sheet and
        # live captures are written there; neither may be clobbered or
        # deleted by a code push. First-time seed: scp Data/can_signal_map.json.
        --exclude='/Data/can_signal_map.json'
        --exclude='/Data/can_signal_map.json.*'
        --exclude='/Data/can_captures/live_*'
    )
fi

RSYNC_FLAGS=(-rlpt --delete --human-readable)
[ "$DRY_RUN" -eq 1 ] && RSYNC_FLAGS+=(--dry-run --itemize-changes)

echo "== syncing $APP_DIR -> $PI_HOST:$PI_PATH =="
[ "$WITH_STATE" -eq 1 ] && echo "   (including Data/ runtime state)"
rsync "${RSYNC_FLAGS[@]}" "${EXCLUDES[@]}" "${EXTRA_EXCLUDES[@]}" \
    -e "ssh -i $PI_KEY" \
    "$APP_DIR/" "$PI_HOST:$PI_PATH/" || exit 1

if [ "$DRY_RUN" -eq 1 ]; then
    echo "(dry run — nothing changed)"
    exit 0
fi

# The launcher and helper scripts must stay executable; the dev checkout may
# sit on a filesystem that cannot express the bit.
ssh -i "$PI_KEY" "$PI_HOST" "chmod +x '$PI_PATH'/Installation/*.sh '$PI_PATH'/Installation/pi-config/*.sh '$PI_PATH'/Tools/*.sh 2>/dev/null; true"

if [ "$RESTART" -eq 0 ]; then
    echo "== done (service left alone) =="
    exit 0
fi

echo "== restarting laladash.service =="
ssh -i "$PI_KEY" "$PI_HOST" '
    systemctl --user restart laladash.service
    sleep 10
    echo "-- state: $(systemctl --user is-active laladash.service) --"
    echo "-- log --"
    cat ~/Desktop/LaLaDash_ERROR.txt 2>/dev/null | tail -25
'
