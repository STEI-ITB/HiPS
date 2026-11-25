#!/usr/bin/env bash
# Run the emulation campaign in a container (image built from docker/Dockerfile).
#
#   bash docker/ojsdn-docker.sh build                 build image ojsdn-wifi:<tag>
#   sudo bash docker/ojsdn-docker.sh host-prep [--cstates]
#                                                      host changes the container needs (recorded, see host-undo);
#                                                      --cstates: GRUB C-state limit as tune-host.sh (reboot)
#   bash docker/ojsdn-docker.sh check                  host + image preflight (no changes)
#   bash docker/ojsdn-docker.sh smoke                  smoke test inside the container
#   bash docker/ojsdn-docker.sh run -- <cmd...>        one-off command in a throw-away container
#   bash docker/ojsdn-docker.sh campaign [--queue "1a 1b 1c 1d 2 3 4 5"]
#                                                      detached campaign container, restarts after crashes/reboots
#   bash docker/ojsdn-docker.sh status|logs|stop
#   sudo bash docker/ojsdn-docker.sh host-undo         revert exactly what host-prep changed
#   bash docker/ojsdn-docker.sh uninstall              remove the campaign container and the image
#
# Container: --privileged --network host (hwsim radios and OVS live in the host kernel), own PID
# namespace (cleanup's pkill only sees the container), host /lib/modules read-only, host debugfs
# (Mininet-WiFi finds the hwsim phys under /sys/kernel/debug/ieee80211), the testbed
# folder bind-mounted at /ojsdn/testbed (results stay on the host, owned by the folder's owner via SUDO_UID/GID
# as with sudo natively). The host must not run Open vSwitch itself.
set -euo pipefail
TESTBED="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${OJSDN_IMAGE:-ojsdn-wifi:paper}"
NAME="${OJSDN_CONTAINER:-ojsdn-wifi-campaign}"
STATE=/var/lib/ojsdn-docker          # host-prep records here what it changed
NMCONF=/etc/NetworkManager/conf.d/90-ojsdn-wifi-unmanaged.conf
CMD="${1:-status}"; shift || true

docker_args() {
  # --init: tini as PID 1 reaps orphans; without it zombies (e.g. hostapd_cli, pkill) pile up and
  # Mininet-WiFi's cleanup loops forever on `pgrep -f hostapd`
  echo --init --privileged --network host \
       -v /lib/modules:/lib/modules:ro -v /sys/kernel/debug:/sys/kernel/debug -v /etc/localtime:/etc/localtime:ro \
       -v "$TESTBED":/ojsdn/testbed \
       -e SUDO_UID="$(stat -c %u "$TESTBED")" -e SUDO_GID="$(stat -c %g "$TESTBED")" \
       -e SHELL=/bin/bash -e OJSDN_LOGLEVEL="${OJSDN_LOGLEVEL:-INFO}" -e OJSDN_LLM_BACKEND="${OJSDN_LLM_BACKEND:-}"
}
need_root() { [[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }; }

check() {
  local bad=0
  ok() { echo "  OK   $*"; }; no() { echo "  NO   $*"; bad=1; }
  command -v docker >/dev/null && ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)" || no "docker missing"
  docker image inspect "$IMAGE" >/dev/null 2>&1 && ok "image $IMAGE" || no "image $IMAGE not built (docker/ojsdn-docker.sh build)"
  modinfo mac80211_hwsim >/dev/null 2>&1 && ok "mac80211_hwsim available for $(uname -r)" || no "mac80211_hwsim missing for $(uname -r)"
  modinfo openvswitch >/dev/null 2>&1 && ok "openvswitch module available" || no "openvswitch module missing"
  if pgrep -x ovs-vswitchd >/dev/null && ! docker ps -q -f name="^$NAME$" | grep -q .; then
    no "host runs ovs-vswitchd (stop it: the container runs its own)"; else ok "no host ovs-vswitchd"; fi
  if systemctl is-active -q NetworkManager 2>/dev/null; then
    [[ -f $NMCONF ]] && ok "NetworkManager ignores emulation interfaces" || no "NetworkManager would manage the hwsim radios (sudo ... host-prep)"
  else ok "no NetworkManager"; fi
  [[ -f "$TESTBED/testbed.$(hostname -s).yaml" ]] && ok "host overlay testbed.$(hostname -s).yaml" \
    || echo "  ..   no testbed.$(hostname -s).yaml: testbed.yaml cores apply"
  return $bad
}

case "$CMD" in
  build)
    cp "$TESTBED/install/wmediumd_ojsdn.py" "$TESTBED/docker/"   # single source of the wmediumd patches
    docker build -t "$IMAGE" "$TESTBED/docker" ;;
  host-prep)
    need_root; mkdir -p "$STATE"
    if systemctl is-active -q NetworkManager 2>/dev/null && [[ ! -f $NMCONF ]]; then
      # same device list as install-emu-host.sh: hwsim radios, AP/STA/switch interfaces of the emulation
      cat > "$NMCONF" <<'NMC'
[keyfile]
unmanaged-devices=driver:mac80211_hwsim;interface-name:hwsim*;interface-name:wlan*;interface-name:*-wlan*;interface-name:*-eth*;interface-name:*-mp*;interface-name:ap*;interface-name:s1;interface-name:ovs-system
NMC
      echo "$NMCONF" >> "$STATE/created-files"; systemctl reload NetworkManager; echo "NetworkManager: emulation interfaces unmanaged"
    fi
    if [[ ! -f "$STATE/modules-loaded-before" ]]; then   # first call only: later runs load them themselves
      touch "$STATE/modules-loaded-before"
      for m in openvswitch mac80211_hwsim; do grep -q "^$m " /proc/modules && echo "$m" >> "$STATE/modules-loaded-before"; done
    fi
    if [[ "${1:-}" == "--cstates" ]] && ! grep -q "max_cstate=1" /etc/default/grub; then
      # C-state limit as tune-host.sh, without its sysctl file (other workloads on the host); needs a reboot
      grep '^GRUB_CMDLINE_LINUX_DEFAULT=' /etc/default/grub > "$STATE/grub-cmdline.before"
      sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 processor.max_cstate=1 intel_idle.max_cstate=1"/' /etc/default/grub
      update-grub >/dev/null 2>&1 && echo "GRUB: C-states limited (reboot required)"
    fi
    echo "host prepared (state in $STATE)" ;;
  host-undo)
    need_root
    docker ps -q -f name="^$NAME$" | grep -q . && { echo "campaign container still running: stop it first" >&2; exit 1; }
    if [[ -f "$STATE/created-files" ]]; then
      while read -r f; do rm -f "$f" && echo "removed $f"; done < "$STATE/created-files"
      systemctl reload NetworkManager 2>/dev/null || true
    fi
    if [[ -f "$STATE/grub-cmdline.before" ]]; then
      line="$(cat "$STATE/grub-cmdline.before")"
      python3 - "$line" <<'PYG'
import re, sys
p = "/etc/default/grub"; s = open(p).read()
open(p, "w").write(re.sub(r"^GRUB_CMDLINE_LINUX_DEFAULT=.*$", lambda m: sys.argv[1], s, count=1, flags=re.M))
PYG
      update-grub >/dev/null 2>&1 && echo "GRUB: kernel command line restored (reboot required)"
    fi
    for m in mac80211_hwsim openvswitch; do   # unload only what was not loaded before host-prep
      grep -qx "$m" "$STATE/modules-loaded-before" 2>/dev/null || { grep -q "^$m " /proc/modules && rmmod "$m" && echo "unloaded $m"; } || true
    done
    rm -rf "$STATE"; echo "host-prep undone" ;;
  check) check ;;
  run)
    [[ "${1:-}" == "--" ]] && shift
    # shellcheck disable=SC2046
    docker run --rm $([ -t 0 ] && echo -it) $(docker_args) "$IMAGE" "$@" ;;
  smoke)
    # shellcheck disable=SC2046
    docker run --rm $(docker_args) "$IMAGE" /opt/venv/bin/python common/smoke_test.py --n_ap 4 --n_sta 4 ;;
  campaign)
    QUEUE="1a 1b 1c 1d 2 3 4 5"; [[ "${1:-}" == "--queue" ]] && QUEUE="$2"
    check || { echo "preflight failed" >&2; exit 1; }
    mkdir -p "$TESTBED/output"
    # After a clean finish the container idles instead of exiting: an exit 0 under unless-stopped
    # restarted the finished campaign every few seconds.
    # shellcheck disable=SC2046
    docker run -d --name "$NAME" --restart unless-stopped $(docker_args) "$IMAGE" \
      bash -c "[ -e output/CAMPAIGN_DONE ] || { ./run-campaign.sh --queue '$QUEUE' >> output/campaign.log 2>&1 && date -Is > output/CAMPAIGN_DONE; }; [ -e output/CAMPAIGN_DONE ] && exec sleep infinity; exit 1"
    echo "started $NAME; follow with: tail -f $TESTBED/output/campaign.log" ;;
  status) docker ps -a -f name="^$NAME$" --format '{{.Names}}  {{.Status}}  {{.Image}}'; tail -3 "$TESTBED/output/campaign.log" 2>/dev/null || true ;;
  logs) docker logs --tail 50 "$NAME" ;;
  stop) docker update --restart no "$NAME" >/dev/null 2>&1 || true; docker stop -t 60 "$NAME" ;;
  uninstall) docker rm -f "$NAME" 2>/dev/null || true; docker rmi "$IMAGE" 2>/dev/null || true; echo "container and image removed" ;;
  *) sed -n '2,18p' "$0"; exit 64 ;;
esac
