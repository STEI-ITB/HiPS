#!/usr/bin/env bash
# Record the software/hardware environment of a campaign into output/env/.
set -uo pipefail
TESTBED="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$TESTBED/output/env"; mkdir -p "$OUT"
{
  echo "# recorded $(date -Is) on $(hostname)"
  uname -a; echo; lscpu; echo; free -g; echo
  echo "mininet-wifi commit: $(cat "$HOME/mnwifi_commit.txt" 2>/dev/null || git -C "$HOME/mininet-wifi" rev-parse HEAD 2>/dev/null)"
  echo "mininet: $(mn --version 2>&1 | head -1)"
  echo "hostapd: $(hostapd -v 2>&1 | head -1)"; echo "wpa_supplicant: $(wpa_supplicant -v 2>&1 | head -1)"
  echo "iw: $(iw --version 2>&1)"; echo "ovs: $(ovs-vsctl --version 2>&1 | head -1)"
  echo "iperf3: $(iperf3 --version 2>&1 | head -1)"; echo "irtt: $(irtt -v 2>&1 | head -1)"
  echo "tc: $(tc -V 2>&1)"; echo "python: $("$TESTBED/.venv/bin/python" --version 2>&1)"
  LLM_HOST="$(sed -n 's/^  host: *\([^ #]*\).*/\1/p' "$TESTBED/testbed.yaml" | head -1)"; LLM_HOST="${LLM_HOST:-http://127.0.0.1:11434}"
  echo "ollama server: $LLM_HOST $(curl -fsS -m 5 "$LLM_HOST/api/version" 2>&1)"
  echo; echo "# pip freeze"; "$TESTBED/.venv/bin/pip" freeze 2>/dev/null
  echo; echo "# ollama models on $LLM_HOST (name digest size)"
  curl -fsS -m 10 "$LLM_HOST/api/tags" | jq -r '.models[] | "\(.name)\t\(.digest)\t\(.size)"' 2>&1
  echo; echo "# kernel cmdline"; cat /proc/cmdline
  echo; echo "# governor"; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null
  echo; echo "# testbed.yaml"; cat "$TESTBED/testbed.yaml"
  HO="$TESTBED/testbed.$(hostname -s).yaml"
  [[ -f "$HO" ]] && { echo; echo "# $(basename "$HO") (host overlay)"; cat "$HO"; }
} > "$OUT/versions.txt" 2>&1
echo "wrote $OUT/versions.txt"
