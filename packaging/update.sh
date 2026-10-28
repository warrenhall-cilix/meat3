#!/bin/bash
# Update meat³ to the latest version from GitHub. Run as your normal user:
#   ~/meat3/packaging/update.sh
# Pulls, reinstalls to /opt/meat3, rebuilds the meat3_smu module only if it changed, restarts meat3d.
# The GUI/TUI update button runs the installed copy (/opt/meat3/update.sh <checkout>) as root via meat3d;
# git then runs as the checkout's owner so .git never gets root-owned files.
set -euo pipefail
SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$SRC"
OWNER="$(stat -c %U "$SRC")"
g() {
    if [[ $EUID -eq 0 && "$OWNER" != root ]]; then runuser -u "$OWNER" -- git -C "$SRC" "$@"; else git -C "$SRC" "$@"; fi
}
echo "== meat3 update $(date '+%F %T') in $SRC"

old="$(g rev-parse HEAD)"
if ! g pull --ff-only; then
    echo
    echo "git pull failed. If you edited files in $SRC, keep your changes with 'git stash' or"
    echo "throw them away with 'git checkout -- .', then run this again."
    exit 1
fi
new="$(g rev-parse HEAD)"

if [[ "$old" == "$new" ]]; then
    echo "== already on the latest version ($(g describe --always)); reinstalling anyway"
else
    echo "== updated $(g rev-parse --short "$old") -> $(g rev-parse --short "$new"):"
    g log --format='   %h %s' "$old..$new"
fi

FLAGS=(--no-deps)
if [[ "$old" != "$new" ]] && ! g diff --quiet "$old" "$new" -- packaging/install.sh; then
    FLAGS=()                                     # installer changed: let it check dependencies again
fi
if [[ "$old" == "$new" ]] || g diff --quiet "$old" "$new" -- kmod/; then
    FLAGS+=(--no-kmod)                           # kernel module unchanged: skip the DKMS rebuild
fi
[[ $EUID -eq 0 ]] && export SUDO_USER="$OWNER"   # installer adds this user (not root) to group meat3
"$SRC/packaging/install.sh" "${FLAGS[@]}"

echo
echo "== done: meat3d restarted with $(g describe --always). Reopen the meat3 window (or meat3-tui)."
