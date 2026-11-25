#!/usr/bin/env bash
# Host tuning (idempotent). Run with sudo:  sudo bash install/tune-host.sh [--disable-services]
#  * GRUB: limit C-states (processor.max_cstate=1 intel_idle.max_cstate=1; on AMD only the first applies)
#    -> needs a reboot
#  * sysctl: /etc/sysctl.d/90-wifi-sla.conf
#  * CPU governor "performance" now and at every boot (ojsdn-governor.service); on amd-pstate-epp also the
#    energy-performance preference and the power-profiles-daemon profile, which would otherwise reset it
#  * --disable-services: also apply campaign mode (install/prep-host.sh: no auto-updates, kernel pinned,
#    no sleep). Prefer running prep-host.sh yourself; it has --check and --undo.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
DISABLE=0; [[ "${1:-}" == "--disable-services" ]] && DISABLE=1
if ! grep -q "max_cstate=1" /etc/default/grub; then
  cp /etc/default/grub "/etc/default/grub.bak.$(date +%s)"
  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 processor.max_cstate=1 intel_idle.max_cstate=1"/' /etc/default/grub
  update-grub
  echo "GRUB updated (reboot required)"
fi
cat > /etc/sysctl.d/90-wifi-sla.conf <<'CONF'
net.core.netdev_max_backlog = 10000
net.core.rmem_max = 26214400
net.core.wmem_max = 26214400
net.ipv4.ip_forward = 1
net.ipv6.conf.all.disable_ipv6 = 1
kernel.sched_autogroup_enabled = 0
CONF
sysctl --system >/dev/null && echo "sysctl applied"
cat > /etc/systemd/system/ojsdn-governor.service <<'UNIT'
[Unit]
Description=Set CPU governor to performance (ojsdn experiments)
After=multi-user.target power-profiles-daemon.service
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'cpupower frequency-set -g performance || for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $$g; done'
ExecStart=-/bin/sh -c 'command -v powerprofilesctl >/dev/null && powerprofilesctl set performance'
ExecStart=-/bin/sh -c 'for e in /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do [ -w $$e ] && echo performance > $$e; done'
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable --now ojsdn-governor.service || true
if [[ $DISABLE -eq 1 ]]; then bash "$(dirname "${BASH_SOURCE[0]}")/prep-host.sh"; fi
echo "tuning done; verify after reboot: cat /proc/cmdline | grep -o max_cstate=1"
