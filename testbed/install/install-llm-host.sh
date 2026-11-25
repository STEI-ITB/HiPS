#!/usr/bin/env bash
# =====================================================================================
# install-llm-host.sh — OPTIONAL second machine (fresh Ubuntu 24.04) that serves Ollama over
# the LAN to the emulation host, or the GPU host of Exp 5e.
#   bash install/install-llm-host.sh [--gpu] [--cores 0-7] [--minimal | --models "m1 m2"]
#   --gpu        expect an NVIDIA GPU (installs the recommended driver if nvidia-smi is missing)
#   --cores SET  pin Ollama to these cores (CPUAffinity); default: all cores
# No firewall rule is set: the testbed is on a trusted LAN and Ollama listens on 0.0.0.0:11434.
# If an Ollama server already answers on :11434 (e.g. a Docker container, as on the paper's GPU node), nothing is
# installed or reconfigured: the models are only pulled into it. From the emulation host this is
# the same as:  bash install/pull-models.sh --host http://<this-ip>:11434 --models qwen3.5:9b
# Then on the emulation host set in testbed.yaml:  llm.host: http://<this-ip>:11434
# (or llm.gpu_host for Exp 5e) and report the extra LAN RTT (~0.3 ms) in the paper.
# =====================================================================================
set -euo pipefail
TESTBED="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GPU=0; CORES=""; PULL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow) echo "--allow is ignored (no firewall on the testbed LAN)"; shift ;;
    --gpu) GPU=1 ;;
    --cores) CORES="$2"; shift ;;
    --minimal) PULL=(--minimal) ;;
    --models) PULL=(--models "$2"); shift ;;
    *) echo "unknown option $1"; exit 64 ;;
  esac
  shift
done
if curl -fsS -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1 && ! systemctl is-active -q ollama; then
  echo "Ollama $(curl -fsS http://127.0.0.1:11434/api/version | jq -r .version) already serves :11434 (not the ollama unit, e.g. Docker): only pulling models"
else
  sudo apt-get update && sudo apt-get install -y curl jq zstd
  if [[ $GPU -eq 1 ]] && ! command -v nvidia-smi >/dev/null; then
    sudo apt-get install -y ubuntu-drivers-common && sudo ubuntu-drivers install
    echo ">>> NVIDIA driver installed: REBOOT, then re-run this script <<<"; exit 0
  fi
  command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
  sudo mkdir -p /etc/systemd/system/ollama.service.d
  {
    echo "[Service]"
    echo 'Environment="OLLAMA_HOST=0.0.0.0:11434" "OLLAMA_KEEP_ALIVE=-1" "OLLAMA_NUM_PARALLEL=1" "OLLAMA_MAX_LOADED_MODELS=2"'
    if [[ -n "$CORES" ]]; then echo "CPUAffinity=${CORES//,/ }"; fi
  } | sudo tee /etc/systemd/system/ollama.service.d/ojsdn.conf >/dev/null
  sudo systemctl daemon-reload && sudo systemctl enable --now ollama && sudo systemctl restart ollama
  for _ in $(seq 1 30); do curl -sf localhost:11434/api/tags >/dev/null && break; sleep 1; done
fi
bash "$TESTBED/install/pull-models.sh" --host http://127.0.0.1:11434 "${PULL[@]}"
[[ $GPU -eq 1 ]] && nvidia-smi --query-gpu=name,memory.total --format=csv || true
echo "LLM host ready: http://$(hostname -I | awk '{print $1}'):11434"
