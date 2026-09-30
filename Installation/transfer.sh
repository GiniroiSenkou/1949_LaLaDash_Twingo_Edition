#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/local.env" ] && . "$HERE/local.env"
TARGET_HOST="${TARGET_HOST:-pi@raspberrypi.local}"
TARGET_DIR="${TARGET_DIR:-/home/pi/Desktop}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PARENT="$(dirname "$ROOT")"

echo "LaLaDash transfer → ${TARGET_HOST}:${TARGET_DIR}/"
echo "Source: ${ROOT}  (parent: ${PARENT})"
echo
echo "  1) Music folder only"
echo "  2) Stems folder only"
echo "  3) Lyrics folder only"
echo "  4) Music + Stems + Lyrics"
echo "  5) Cars folder only            (→ inside LaLaDash on target)"
echo "  6) Whole LaLaDash + media      (LaLaDash + Music + Stems + Lyrics)"
echo "  7) LaLaDash code only          (excludes: Installation/ Cars/ cache/ Data/)"
echo "  8) Installation folder only"
echo "  9) music.json only"
echo "  q) Quit"
echo
read -rp "Choice: " CHOICE

# Ensure sibling media folders exist on the source so optional bundles
# never fail just because the user hasn't generated lyrics yet, etc.
mkdir -p "$PARENT/Music" "$PARENT/Stems" "$PARENT/Lyrics"

# Make sure the LaLaDash folder exists on the target before we scp things
# *into* it (e.g. Cars). Otherwise scp -r dumps to the parent and the user
# gets the folder in the wrong place.
ensure_remote_laladash() {
    ssh "${TARGET_HOST}" "mkdir -p '${TARGET_DIR}/LaLaDash'"
}

case "$CHOICE" in
    1)
        echo "→ Transferring Music..."
        scp -r "$PARENT/Music" "${TARGET_HOST}:${TARGET_DIR}/"
        ;;
    2)
        echo "→ Transferring Stems..."
        scp -r "$PARENT/Stems" "${TARGET_HOST}:${TARGET_DIR}/"
        ;;
    3)
        echo "→ Transferring Lyrics..."
        scp -r "$PARENT/Lyrics" "${TARGET_HOST}:${TARGET_DIR}/"
        ;;
    4)
        echo "→ Transferring Music + Stems + Lyrics..."
        scp -r "$PARENT/Music" "$PARENT/Stems" "$PARENT/Lyrics" "${TARGET_HOST}:${TARGET_DIR}/"
        ;;
    5)
        echo "→ Transferring Cars → ${TARGET_HOST}:${TARGET_DIR}/LaLaDash/Cars/"
        ensure_remote_laladash
        scp -r "$ROOT/Cars" "${TARGET_HOST}:${TARGET_DIR}/LaLaDash/"
        ;;
    6)
        echo "→ Transferring whole LaLaDash + media siblings..."
        # LaLaDash code+resources → ${TARGET_DIR}/LaLaDash/
        # Music/Stems/Lyrics      → ${TARGET_DIR}/Music, /Stems, /Lyrics (siblings)
        scp -r "$ROOT" "${TARGET_HOST}:${TARGET_DIR}/"
        scp -r "$PARENT/Music" "$PARENT/Stems" "$PARENT/Lyrics" "${TARGET_HOST}:${TARGET_DIR}/"
        ;;
    7)
        echo "→ Transferring LaLaDash code → ${TARGET_HOST}:${TARGET_DIR}/LaLaDash/"
        echo "   (excluding top-level Installation/ Cars/ cache/ Data/)"
        ensure_remote_laladash
        rsync -av --delete \
            --exclude='/Installation/' \
            --exclude='/Cars/' \
            --exclude='/cache/' \
            --exclude='/Data/' \
            --exclude='/.venv/' \
            --exclude='/.idea/' \
            --exclude='/.vscode/' \
            --exclude='/.claude/' \
            --exclude='__pycache__/' \
            --exclude='.git/' \
            --exclude='*.pyc' \
            "$ROOT/" "${TARGET_HOST}:${TARGET_DIR}/LaLaDash/"
        ;;
    8)
        echo "→ Transferring Installation folder..."
        ensure_remote_laladash
        rsync -av \
            --exclude='__pycache__' \
            --exclude='*.pyc' \
            --exclude='env/' \
            --exclude='LaLaEnv/' \
            "$ROOT/Installation/" "${TARGET_HOST}:${TARGET_DIR}/LaLaDash/Installation/"
        ;;
    9)
        echo "→ Transferring music.json..."
        ensure_remote_laladash
        ssh "${TARGET_HOST}" "mkdir -p '${TARGET_DIR}/LaLaDash/Data'"
        scp "$ROOT/Data/music.json" "${TARGET_HOST}:${TARGET_DIR}/LaLaDash/Data/"
        ;;
    q|Q)
        echo "Cancelled."
        exit 0
        ;;
    *)
        echo "Invalid choice: $CHOICE"
        exit 1
        ;;
esac

echo "Done."
