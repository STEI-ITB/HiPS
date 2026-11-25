#!/usr/bin/env bash
# Experiment 10b: offline intent-change benchmark (no emulator), one model after another, then the rescoring.
#   bench/run_intent_bench.sh OUTDIR [MODEL ...]
# Defaults to the four models of the paper. Needs the testbed venv (testbed/requirements.txt) and an Ollama
# server: llm.host in testbed/testbed.yaml, or OLLAMA_HOST_OVERRIDE=http://<gpu-host>:11434.
# 30 calls per arm and policy, synthetic states with rng seed 202, as in the paper.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TESTBED="$HERE/../testbed"
OUT="${1:?usage: run_intent_bench.sh OUTDIR [MODEL ...]}"; shift
MODELS=("$@"); [[ ${#MODELS[@]} -gt 0 ]] || MODELS=(qwen3.5:4b qwen3.5:9b gemma4:26b-a4b-it-q4_K_M phi4)
PY="${PYTHON:-$TESTBED/.venv/bin/python}"
mkdir -p "$OUT"
for m in "${MODELS[@]}"; do
  echo "=== $m $(date -Is)"
  "$PY" -I "$HERE/intent_bench.py" "$TESTBED" "$OUT/intent_bench_${m//:/-}.csv" "$m" 30
done
"$PY" -I "$HERE/rescore_intent.py" "$TESTBED" "$OUT/rescored_all.csv" "$OUT"/intent_bench_*.csv
echo "=== done $(date -Is)"
