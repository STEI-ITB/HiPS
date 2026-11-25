#!/usr/bin/env bash
# =====================================================================================
# run-campaign.sh — run a queue of experiments unattended, with auto-resume and retries.
#
#   sudo -E ./run-campaign.sh [--queue "1a 1b 1c 1d 2 3 4 5"] [--cloud] [--passes 3] [--no-process]
#
# Each queue item is "<exp>" or "<exp><sub>" (e.g. 5a). Items are run in order through
# experiment-N/run.sh (which skips finished runs). Items that end with pending runs (exit 3,
# e.g. after a rate-limit stop) are retried in up to --passes passes. A fatal environment error
# (exit 2) stops the campaign. Afterwards process-result.py rebuilds results.xlsx.
# Safe to kill at any time and to start again (also used by install/ojsdn-campaign.service).
# =====================================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUEUE="1a 1b 1c 1d 2 3 4 5"; PASSES=3; PROCESS=1; EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --queue) QUEUE="$2"; shift ;;
    --passes) PASSES="$2"; shift ;;
    --cloud) EXTRA+=(--cloud) ;;
    --no-process) PROCESS=0 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 64 ;;
  esac
  shift
done
[[ $EUID -eq 0 ]] || { echo "run with sudo -E (emulation needs root)"; exit 1; }
mkdir -p "$HERE/output"
echo "[$(date +%F' '%T)] campaign start: queue='$QUEUE' ${EXTRA[*]:-}"
bash "$HERE/install/record-env.sh" >/dev/null 2>&1 || true

rc_all=0
for pass in $(seq 1 "$PASSES"); do
  pending=0
  for item in $QUEUE; do
    exp="${item%%[!0-9]*}"; sub="${item#"$exp"}"   # 1-2 digit experiment numbers (experiment-11)
    args=(); [[ -n "$sub" ]] && args+=("$item")
    echo "[$(date +%F' '%T)] pass $pass: experiment $item"
    set +e; "$HERE/experiment-$exp/run.sh" "${args[@]}" "${EXTRA[@]}"; rc=$?; set -e
    case "$rc" in
      0) ;;
      3) pending=1 ;;
      2) echo "FATAL environment error during $item — fix it, then re-run this script (progress is kept)"; exit 2 ;;
      75) echo "another campaign holds the lock; exiting"; exit 75 ;;
      *) echo "experiment $item exited with $rc"; pending=1 ;;
    esac
  done
  [[ $pending -eq 0 ]] && break
  echo "[$(date +%F' '%T)] pass $pass left pending runs; next pass"
  rc_all=3
done
if [[ $PROCESS -eq 1 ]]; then
  PY="${OJSDN_PYTHON:-$HERE/.venv/bin/python}"
  "$PY" "$HERE/process-result.py" --output-dir "$HERE/output" --xlsx "$HERE/results.xlsx" || true
  [[ -n "${SUDO_UID:-}" ]] && chown "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$HERE/results.xlsx" 2>/dev/null || true
fi
if [[ $pending -eq 0 ]]; then echo "[$(date +%F' '%T)] campaign complete"; exit 0; fi
echo "[$(date +%F' '%T)] campaign ended with pending runs (see output/experiment-*/failed_runs.csv)"
exit "$rc_all"
