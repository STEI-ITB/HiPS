#!/usr/bin/env bash
# Experiment 4 runner — see the README. Usage:
#   sudo -E ./experiment-4/run.sh [SUB] [--dry-run] [--status] [--fresh] [--cloud] [--only TAG]
# Auto-resume is the default; see common/runlib.sh for all options and exit codes.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/runlib.sh
source "$HERE/../common/runlib.sh"
runlib_main 4 "$@"
