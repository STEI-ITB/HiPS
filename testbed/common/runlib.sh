# shellcheck shell=bash
# Shared run loop for every experiment-N/run.sh (sourced, not executed).
#
#   run.sh [SUB] [--dry-run] [--status] [--fresh] [--resume] [--cloud|--local] [--only TAG] [--max-attempts N]
#
# * --cloud runs the experiment's cost-efficient Ollama Cloud variant (config.yaml `cloud:` block,
#   OLLAMA_API_KEY must be exported; use `sudo -E`). LLM runs get a `_cloud` tag suffix; non-LLM
#   runs (B0/B1/none_rule) are shared with the local campaign and not repeated.
# * Auto-resume is the DEFAULT: finished runs (DONE marker + summary row) are skipped, partial run
#   folders are quarantined and re-run, runs that failed max_attempts times are skipped and listed
#   in output/experiment-N/failed_runs.csv. `--resume` is accepted for compatibility (no-op).
# * `--fresh` archives the selected runs' results and starts them again.
# * Exit codes of a run: 0 ok, 1 failed (retried up to max_attempts), 2 fatal (stop),
#   75 temporary/rate-limited (cool down, retry, attempt not counted).
# * run.sh exits 0 when nothing is pending, 3 when runs are still pending (e.g. stopped early),
#   2 on a fatal environment error, 130 when interrupted.

runlib_main() {
  local EXP="$1"; shift
  local SUB="" DRY=0 STATUS=0 FRESH=0 ONLY="" MAXATT=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) DRY=1 ;;
      --status) STATUS=1 ;;
      --fresh) FRESH=1 ;;
      --resume) : ;;                       # default behaviour
      --cloud) export OJSDN_LLM_BACKEND=cloud ;;
      --local) export OJSDN_LLM_BACKEND=local ;;
      --only) ONLY="$2"; shift ;;
      --max-attempts) MAXATT="$2"; shift ;;
      -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; return 0 ;;
      [0-9][a-z]|[0-9][0-9][a-z]) SUB="$1" ;;
      *) echo "unknown argument: $1" >&2; return 64 ;;
    esac
    shift
  done

  local TESTBED; TESTBED="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  cd "$TESTBED"
  local PY="${OJSDN_PYTHON:-}"
  if [[ -z "$PY" ]]; then
    if [[ -x "$TESTBED/.venv/bin/python" ]]; then PY="$TESTBED/.venv/bin/python"; else PY="$(command -v python3)"; fi
  fi
  export PYTHONUNBUFFERED=1
  local SUBARG=(); [[ -n "$SUB" ]] && SUBARG=(--sub "$SUB")

  if [[ $STATUS -eq 1 ]]; then
    "$PY" common/campaign.py status --exp "$EXP" "${SUBARG[@]}"
    "$PY" common/campaign.py estimate --exp "$EXP" "${SUBARG[@]}" | sed 's/^/pending LLM usage: /'
    return 0
  fi

  if [[ $DRY -eq 1 ]]; then
    local n=0
    echo "# exp $EXP ${SUB:+sub $SUB}: run_tag | entry | status | privileges | sub_exps"
    while IFS=$'\t' read -r tag entry cfgp st priv subs; do
      n=$((n + 1)); printf '%4d  %-60s %-28s %-11s %-4s %s\n' "$n" "$tag" "$entry" "$st" "$priv" "$subs"
    done < <("$PY" common/campaign.py plan --exp "$EXP" "${SUBARG[@]}")
    echo "# total runs: $n"
    "$PY" common/campaign.py status --exp "$EXP" "${SUBARG[@]}"
    return 0
  fi

  # ---- one campaign at a time per testbed folder (the emulator is a host-wide singleton)
  mkdir -p output
  exec 9>"$TESTBED/output/.campaign.lock"
  if ! flock -n 9; then echo "another campaign is running (output/.campaign.lock)" >&2; return 75; fi

  if [[ $FRESH -eq 1 ]]; then
    local ONLYARG=(); [[ -n "$ONLY" ]] && ONLYARG=(--only "$ONLY")
    "$PY" common/campaign.py fresh --exp "$EXP" "${SUBARG[@]}" "${ONLYARG[@]}"
  fi

  local PLAN; PLAN="$(mktemp)"
  "$PY" common/campaign.py plan --exp "$EXP" "${SUBARG[@]}" > "$PLAN"
  if awk -F'\t' '$4!="done" && $4!="failed" && $5=="root"' "$PLAN" | grep -q . && [[ $EUID -ne 0 ]]; then
    echo "pending runs need root: sudo -E $0 ${SUB} ..." >&2; rm -f "$PLAN"; return 2
  fi
  local COOL; COOL="$("$PY" -c 'from common import config as C; print(int(C.load_base().get("llm",{}).get("cloud_budget",{}).get("cooldown_s",900)))')"
  local MAXA="${MAXATT:-$("$PY" -c 'from common import config as C; print(int(C.load_base().get("max_attempts",3)))')}"
  local EXPDIR="output/experiment-$EXP"

  trap 'echo "interrupted"; rm -f "$PLAN"; exit 130' INT TERM
  local total=0 ok=0 failed=0 skipped=0
  # the plan is read on fd 3 and children get /dev/null as stdin, so no child can eat plan lines
  while IFS=$'\t' read -r tag entry cfgp st priv subs <&3; do
    total=$((total + 1))
    [[ -n "$ONLY" && "$tag" != "$ONLY" ]] && continue
    case "$st" in
      done) skipped=$((skipped + 1)); continue ;;
      failed) echo "[skip] $tag failed $MAXA times (see $EXPDIR/failed_runs.csv)"; continue ;;
      resummarize)
        echo "[resummarize] $tag"
        "$PY" common/run_one.py --run-config "$cfgp" --summarize-only </dev/null || true
        continue ;;
    esac
    local temp=0
    while :; do
      local att; att="$("$PY" -c "from common import runstate as R; from pathlib import Path; print(R.get_attempts(Path('$EXPDIR'), '$tag'))")"
      if [[ "$att" -ge "$MAXA" ]]; then
        "$PY" -c "from common import runstate as R; from pathlib import Path; R.record_failure(Path('$EXPDIR'), '$tag', 'max attempts')"
        failed=$((failed + 1)); break
      fi
      echo "[$(date +%F' '%T)] run $total: $tag (attempt $((att + 1))/$MAXA)"
      set +e; "$PY" "$entry" --run-config "$cfgp" </dev/null; local rc=$?; set -e
      case "$rc" in
        0) ok=$((ok + 1)); break ;;
        2) echo "FATAL environment error in $tag — fix the setup (see $EXPDIR/$tag/*.log) and re-run; progress is kept." >&2
           rm -f "$PLAN"; return 2 ;;
        75) temp=$((temp + 1))
            if [[ $temp -gt 48 ]]; then echo "still rate-limited after 48 cool-downs; stopping" >&2; rm -f "$PLAN"; return 3; fi
            echo "[cool-down] $tag rate-limited/temporary failure; sleeping ${COOL}s (not counted as an attempt)"
            sleep "$COOL" ;;
        *) "$PY" -c "from common import runstate as R; from pathlib import Path; R.bump_attempts(Path('$EXPDIR'), '$tag')"
           echo "[retry] $tag exited with $rc" ;;
      esac
    done
  done 3< "$PLAN"
  rm -f "$PLAN"
  echo "exp $EXP ${SUB}: $ok newly completed, $skipped already done, $failed failed permanently (of $total)"
  local pend; pend="$("$PY" common/campaign.py status --exp "$EXP" "${SUBARG[@]}" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["pending"])')"
  [[ "$pend" -eq 0 ]] && return 0 || return 3
}
