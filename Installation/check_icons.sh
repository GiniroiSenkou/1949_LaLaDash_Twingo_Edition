#!/usr/bin/env bash
# check_icons.sh — audit QML icon references against GUI/Icons/
# Run from project root:  bash Installation/check_icons.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ICONS="$ROOT/GUI/Icons"
QML_DIRS=("$ROOT/Styles" "$ROOT/main.qml")

MISSING=0
FOUND=0

check_path() {
    local rel="$1"
    # strip leading slash if present
    rel="${rel#/}"
    local full="$ICONS/$rel"
    if [ -f "$full" ]; then
        FOUND=$((FOUND + 1))
    else
        echo "MISSING: GUI/Icons/$rel"
        MISSING=$((MISSING + 1))
    fi
}

# Extract all  iconsPath + "/..." strings from QML files
for dir in "${QML_DIRS[@]}"; do
    [ -e "$dir" ] || continue
    while IFS= read -r line; do
        # match  iconsPath + "/some/path"  or  iconsPath + '/some/path'
        while [[ "$line" =~ iconsPath[[:space:]]*\+[[:space:]]*[\"\']([^\"\']+)[\"\'] ]]; do
            path="${BASH_REMATCH[1]}"
            # skip dynamic expressions that contain modelData, variable refs, etc.
            # Only check paths with a file extension and no dynamic parts
            if [[ "$path" =~ \.[a-zA-Z]{2,5}$ && "$path" != *'$'* && "$path" != *modelData* && "$path" != *Music\.* && "$path" != *Nav\.* && "$path" != *Modding\.* ]]; then
                check_path "$path"
            fi
            # remove the match and continue searching the same line
            line="${line#*iconsPath}"
        done
    done < <(grep -rh "iconsPath" "$dir" 2>/dev/null)
done

echo ""
echo "Results: $FOUND OK, $MISSING MISSING"
if [ "$MISSING" -gt 0 ]; then
    echo "Ship the missing files to the Pi or they will show as grey squares."
    exit 1
else
    echo "All referenced icons found."
    exit 0
fi
