#!/usr/bin/env bash
# Container entrypoint: kernel modules from the host's /lib/modules, Open vSwitch daemons (no init
# system in the container), then the command. Mininet-WiFi loads mac80211_hwsim itself per run.
set -euo pipefail
modprobe openvswitch 2>/dev/null || grep -q '^openvswitch ' /proc/modules \
  || { echo "openvswitch kernel module unavailable (is /lib/modules mounted?)" >&2; exit 2; }
modinfo mac80211_hwsim >/dev/null 2>&1 || { echo "mac80211_hwsim not in /lib/modules/$(uname -r)" >&2; exit 2; }
# the host must not run its own ovs-vswitchd: two daemons would fight over the kernel datapath
if [[ -S /run/openvswitch/db.sock ]] && ! pgrep -x ovsdb-server >/dev/null; then rm -f /run/openvswitch/*; fi
/usr/share/openvswitch/scripts/ovs-ctl start --system-id=random --no-monitor >/dev/null
ovs-vsctl --timeout=10 show >/dev/null
exec "$@"
