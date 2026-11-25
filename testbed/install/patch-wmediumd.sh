#!/usr/bin/env bash
# Rebuild wmediumd with the ojsdn patches (install/wmediumd_ojsdn.py; idempotent). All are opt-in through
# environment variables that topo.py sets from the run config, so unset they leave wmediumd unchanged:
#  * OJSDN_WMD_RCVBUF   hwsim netlink receive buffer (medium_rcvbuf_kb). libnl's 32 KiB dropped ~500k frames
#                       per 4-AP run (1-2% loss on every flow); 1 MiB removed them.
#  * OJSDN_WMD_PER_CHANNEL  contention per channel (medium_per_channel); stock wmediumd serialises all radios
#                       in one medium whatever their channel.
#  * OJSDN_WMD_MAXQ_US  bounded queue (medium_max_queue_ms); stock wmediumd queues without limit, and once
#                       offered airtime exceeds real time the emulation stalls with minute-long delays.
#  * OJSDN_WMD_STATS    per-second counters file (medium_frames / medium_maxq_drops in run_meta.json).
#
# Usage: bash install/patch-wmediumd.sh [--src DIR] [--check]
#   --src DIR  wmediumd source tree (default: ~/wmediumd, where Mininet-WiFi's util/install.sh clones it; cloned
#              from https://github.com/ramonfontes/wmediumd if missing). It is first checked out at the commit
#              the paper used (WMEDIUMD_COMMIT, default 63e9fc9), so every host builds the same source.
#   --check    only report whether the installed binary carries the patch
set -euo pipefail
SRC="$HOME/wmediumd"
CHECK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --src) SRC="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

BIN="$(command -v wmediumd || echo /usr/bin/wmediumd)"
patched_bin() { [[ -x "$BIN" ]] && [[ $(strings "$BIN" | grep -c OJSDN_WMD_MAXQ_US) -gt 0 ]]; }
if [[ $CHECK -eq 1 ]]; then
  if patched_bin; then echo "wmediumd: patched ($BIN)"; else echo "wmediumd: NOT patched ($BIN)"; exit 1; fi
  exit 0
fi
if patched_bin; then echo "wmediumd: already patched ($BIN)"; exit 0; fi
COMMIT="${WMEDIUMD_COMMIT:-63e9fc995cfb260065c7a0d4c2b14f715f224436}"
[[ -d "$SRC/.git" ]] || git clone -q https://github.com/ramonfontes/wmediumd.git "$SRC"
S=""; [[ -w "$SRC/.git" ]] || S=sudo   # util/install.sh clones it as root
GIT=(git -c safe.directory='*' -C "$SRC")
# util/install.sh makes a shallow clone: fetch the pinned commit by its hash if it is not there
$S "${GIT[@]}" cat-file -e "$COMMIT^{commit}" 2>/dev/null || $S "${GIT[@]}" fetch -q --depth 1 origin "$COMMIT"
$S "${GIT[@]}" checkout -q -f "$COMMIT"   # -f: a patch left by an earlier attempt is re-applied below
C="$SRC/wmediumd/wmediumd.c"
[[ -f "$C" ]] || { echo "wmediumd source not found: $C (use --src)" >&2; exit 1; }

$S python3 -I "$(dirname "${BASH_SOURCE[0]}")/wmediumd_ojsdn.py" "$C"

if pgrep -x wmediumd >/dev/null; then echo "wmediumd is running (experiment active?) - stop it first" >&2; exit 1; fi
$S make -C "$SRC/wmediumd" clean >/dev/null
$S make -C "$SRC/wmediumd" >/dev/null 2>&1 || { echo "wmediumd build failed (run: make -C $SRC/wmediumd)" >&2; exit 1; }
[[ -f "$BIN.orig" ]] || sudo cp -a "$BIN" "$BIN.orig"
sudo install -m 755 "$SRC/wmediumd/wmediumd" "$BIN"
patched_bin && echo "wmediumd: patched and installed ($BIN; original kept as $BIN.orig)"
