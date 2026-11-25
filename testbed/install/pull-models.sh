#!/usr/bin/env bash
# Pull every local SLM used by the experiments and record their digests.
#   bash install/pull-models.sh [--host http://<ip>:11434] [--minimal | --models "m1 m2"]
#   --host    : pull into a remote Ollama server (e.g. the GPU host's Docker container); works
#               without the ollama CLI (HTTP API), so it can run from any machine on the LAN
#   --minimal : only qwen3.5:4b (default planner) + qwen3.5:0.8b (slm sub-agents)
#   --models  : exactly these models (Exp 5e on the GPU host only needs qwen3.5:9b)
# Disk: ~9 GB for all five models (Q4_K_M). Re-running only downloads missing/updated layers.
set -euo pipefail
TESTBED="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS=(qwen3.5:4b qwen3.5:2b phi3:mini qwen3.5:9b qwen3.5:0.8b)
HOST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift ;;
    --minimal) MODELS=(qwen3.5:4b qwen3.5:0.8b) ;;
    --models) read -r -a MODELS <<<"$2"; shift ;;
    *) echo "unknown option $1"; exit 64 ;;
  esac
  shift
done
URL="${HOST:-${OLLAMA_HOST:-http://127.0.0.1:11434}}"; [[ "$URL" == http* ]] || URL="http://$URL"
curl -fsS -m 10 "$URL/api/version" >/dev/null || { echo "no Ollama server at $URL"; exit 1; }
pull() {
  if command -v ollama >/dev/null; then OLLAMA_HOST="$URL" ollama pull "$1"
  else curl -fsS "$URL/api/pull" -d "{\"model\":\"$1\",\"stream\":false}" | jq -e '.status == "success"' >/dev/null; fi
}
for m in "${MODELS[@]}"; do
  echo "== pulling $m into $URL"
  ok=0
  for try in 1 2 3; do pull "$m" && { ok=1; break; }; echo "retry $try"; sleep 10; done
  [[ $ok -eq 1 ]] || { echo "could not pull $m"; exit 1; }
done
mkdir -p "$TESTBED/output/env"
OUT="$TESTBED/output/env/ollama_models.txt"
[[ -n "$HOST" ]] && OUT="$TESTBED/output/env/ollama_models_$(sed -E 's#^https?://##; s#[^A-Za-z0-9.]+#_#g' <<<"$HOST").txt"
{ echo "# $URL ollama $(curl -fsS "$URL/api/version" | jq -r .version)"
  curl -fsS "$URL/api/tags" | jq -r '.models[] | "\(.name)\t\(.digest)\t\(.size)"'; } | tee "$OUT"
# structured-output sanity check (temperature 0, fixed seed)
curl -s "$URL/api/chat" -d '{
  "model":"'"${MODELS[0]}"'","stream":false,"options":{"temperature":0,"seed":1,"num_thread":4},
  "format":{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]},
  "messages":[{"role":"user","content":"Return ok=true as JSON."}]}' | jq -r .message.content
