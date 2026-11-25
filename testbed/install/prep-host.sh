#!/usr/bin/env bash
# =====================================================================================
# prep-host.sh — put a testbed machine into "campaign mode" (days of unattended runs without
# updates, reboots into another kernel or sleep), report it, or take it out again.
#     sudo bash install/prep-host.sh [--headless]   apply (idempotent; reboot afterwards)
#     bash install/prep-host.sh --check             report only (no sudo needed, changes nothing)
#     sudo bash install/prep-host.sh --undo         restore the state recorded by the first apply
# Apply does:
#   * automatic updates off: APT::Periodic::* = 0 (overrides 20auto-upgrades), apt-daily{,-upgrade},
#     fwupd-refresh, motd-news, ua, update-notifier, man-db timers disabled, unattended-upgrades
#     disabled, packagekit masked, snap refreshes held
#   * kernel pinned to the RUNNING kernel: apt-mark hold of its image/modules/headers/tools packages,
#     of the kernel metapackages (no new ABI gets pulled in) and of NVIDIA driver packages, and
#     GRUB_DEFAULT = that exact menu entry (boots it even if another kernel gets installed)
#   * GRUB_RECORDFAIL_TIMEOUT=5: boots on its own after a crash or power cut
#   * no sleep: sleep/suspend/hibernate targets masked
#   * kernel.panic=10: reboot 10 s after a kernel panic (the campaign unit then resumes the queue)
#   * --headless: boot to multi-user.target (no GNOME session competing for CPU) from next boot
# State before the first apply: /var/lib/ojsdn/ (used by --undo).
# =====================================================================================
set -euo pipefail
MODE=apply; HEADLESS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE=check ;;
    --undo) MODE=undo ;;
    --headless) HEADLESS=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 64 ;;
  esac
  shift
done
[[ $MODE == check || $EUID -eq 0 ]] || { echo "run with sudo (or use --check)"; exit 1; }

KVER="$(uname -r)"; ABI="${KVER%-*}"            # 7.0.0-42-generic -> 7.0.0-42
STATE_DIR=/var/lib/ojsdn; STATE="$STATE_DIR/prep-host.state"; HELD="$STATE_DIR/prep-host.held"
APT_CONF=/etc/apt/apt.conf.d/99ojsdn-no-auto-updates
SYSCTL_CONF=/etc/sysctl.d/91-ojsdn-campaign.conf
TIMERS=(apt-daily.timer apt-daily-upgrade.timer fwupd-refresh.timer motd-news.timer ua-timer.timer
        update-notifier-download.timer update-notifier-motd.timer man-db.timer)
SERVICES=(unattended-upgrades.service)
MASKS=(packagekit.service sleep.target suspend.target hibernate.target hybrid-sleep.target
       suspend-then-hibernate.target)

unit_exists() { [[ -n "$(systemctl list-unit-files --no-legend "$1" 2>/dev/null)" ]]; }
unit_state() { systemctl is-enabled "$1" 2>/dev/null || true; }
grub_get() { sed -n "s/^$1=//p" /etc/default/grub | tail -1 | tr -d '"'; }
grub_set() {  # KEY VALUE: replace or append in /etc/default/grub
  if grep -q "^$1=" /etc/default/grub; then sed -i "s|^$1=.*|$1=\"$2\"|" /etc/default/grub
  else echo "$1=\"$2\"" >> /etc/default/grub; fi
}
# installed packages that belong to the running kernel, the kernel metapackages and NVIDIA drivers
kernel_pkgs() {
  dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' 'linux-*' 'nvidia-*' 'libnvidia-*' 2>/dev/null \
    | awk '$1 ~ /^[ih]i$/{print $2}' \
    | grep -E -- "-${ABI//./\\.}(-|$)|^linux-(generic|image-generic|headers-generic|tools-generic|modules-extra-generic|virtual|image-virtual|headers-virtual)(-hwe-[0-9.]+(-edge)?)?$|^(nvidia-|libnvidia-|linux-(modules|objects|signatures)-nvidia-)" \
    || true
}
# GRUB id of "Advanced options > Ubuntu, with Linux $KVER" (grub.cfg is root-only on 24.04)
grub_entry() {
  local cfg=/boot/grub/grub.cfg sub ent
  sub=$(grep -oP "menuentry_id_option '\Kgnulinux-advanced-[^']+" "$cfg" | head -1) || true
  ent=$(grep -oP "menuentry_id_option '\Kgnulinux-${KVER//./\\.}-advanced-[^']+" "$cfg" | head -1) || true
  [[ -n "$sub" && -n "$ent" ]] && echo "$sub>$ent"
}

# ------------------------------------------------------------------ check (also printed after apply)
check() {
  local bad=0 u s v p f pat ver
  ok()  { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
  no()  { printf '  \033[31mNO\033[0m   %s\n' "$*"; bad=$((bad + 1)); }
  inf() { printf '  ..   %s\n' "$*"; }
  echo "campaign-mode check on $(hostname), kernel $KVER"
  for v in Update-Package-Lists Unattended-Upgrade; do
    s=$(apt-config dump | sed -n "s/^APT::Periodic::$v \"\(.*\)\";/\1/p" | tail -1)
    [[ "${s:-0}" == 0 ]] && ok "APT::Periodic::$v = ${s:-unset}" || no "APT::Periodic::$v = $s"
  done
  for u in "${TIMERS[@]}" "${SERVICES[@]}"; do
    unit_exists "$u" || continue
    s=$(unit_state "$u")
    if [[ "$s" == enabled ]] || systemctl is-active -q "$u"; then no "$u is $s/$(systemctl is-active "$u")"; else ok "$u $s"; fi
  done
  for u in "${MASKS[@]}"; do
    unit_exists "$u" || continue
    s=$(unit_state "$u"); [[ "$s" == masked ]] && ok "$u masked" || no "$u is $s (not masked)"
  done
  if command -v snap >/dev/null; then
    s=$(snap refresh --time 2>/dev/null | sed -n 's/^hold: //p')
    if [[ -n "$s" ]]; then ok "snap refreshes held ($s)"; else no "snap refreshes not held"; fi
  fi
  local held; held=$(apt-mark showhold)
  for p in $(kernel_pkgs); do grep -qx "$p" <<<"$held" && ok "hold $p" || no "not held: $p"; done
  for p in $held; do dpkg-query -W -f='${db:Status-Abbrev}' "$p" 2>/dev/null | grep -qE '^[ih]i' || inf "stale hold (not installed): $p"; done
  v=$(grub_get GRUB_DEFAULT)
  [[ "$v" == *"gnulinux-$KVER-advanced-"* ]] && ok "GRUB_DEFAULT -> $KVER" || no "GRUB_DEFAULT=$v (not pinned to $KVER)"
  grep -q "vmlinuz-$KVER " /proc/cmdline && ok "booted kernel is $KVER" || no "booted kernel differs from $KVER"
  v=$(grub_get GRUB_RECORDFAIL_TIMEOUT)
  [[ -n "$v" && "$v" -ge 0 && "$v" -le 30 ]] && ok "GRUB_RECORDFAIL_TIMEOUT=$v" || no "GRUB_RECORDFAIL_TIMEOUT=${v:-unset} (default 30 s, or wait forever on some setups)"
  v=$(sysctl -n kernel.panic); [[ "$v" -gt 0 ]] && ok "kernel.panic=$v" || no "kernel.panic=$v (no auto-reboot after a panic)"
  inf "default target: $(systemctl get-default)"
  [[ -f /var/run/reboot-required ]] && no "reboot required (/var/run/reboot-required)" || ok "no pending reboot"
  # apt version pins whose pattern does not match the installed kernel do nothing; point them out
  ver=$(dpkg-query -W -f='${Version}' "linux-image-$KVER" 2>/dev/null || true)
  for f in /etc/apt/preferences.d/*; do
    [[ -f "$f" ]] || continue
    while read -r pat; do
      # shellcheck disable=SC2053  # glob match on purpose: apt pins use glob patterns
      [[ -n "$ver" && "$ver" == $pat ]] || inf "$f: 'Pin: version $pat' matches no installed kernel ($ver) - no effect"
    done < <(sed -n 's/^Pin: version //p' "$f")
  done
  echo "  -> $bad item(s) not in campaign mode"
  return $(( bad > 0 ))
}

record_state() {
  [[ -f "$STATE" ]] && { echo "state already recorded in $STATE (kept from the first apply)"; return; }
  mkdir -p "$STATE_DIR"
  cp /etc/default/grub "$STATE_DIR/grub.default.before"
  {
    echo "# recorded $(date -Is) by prep-host.sh on $(hostname), kernel $KVER"
    for u in "${TIMERS[@]}" "${SERVICES[@]}" "${MASKS[@]}"; do
      if unit_exists "$u"; then echo "unit $u $(unit_state "$u")"; fi
    done
    echo "default_target $(systemctl get-default)"
    if command -v snap >/dev/null && [[ -n "$(snap refresh --time 2>/dev/null | sed -n 's/^hold: //p')" ]]; then
      echo "snap_hold yes"; else echo "snap_hold no"; fi
  } > "$STATE"
  : > "$HELD"
  echo "state recorded in $STATE"
}

apply() {
  record_state
  echo "== automatic updates off"
  cat > "$APT_CONF" <<'EOF'
// ojsdn campaign mode (install/prep-host.sh): no automatic apt activity. --undo removes this file.
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::AutocleanInterval "0";
APT::Periodic::Unattended-Upgrade "0";
EOF
  local u p
  for u in "${TIMERS[@]}" "${SERVICES[@]}"; do
    if unit_exists "$u"; then systemctl disable --now "$u" 2>/dev/null || true; fi
  done
  systemctl stop apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
  for u in "${MASKS[@]}"; do
    if unit_exists "$u"; then systemctl stop "$u" 2>/dev/null || true; systemctl mask "$u" >/dev/null; fi
  done
  if command -v snap >/dev/null; then snap refresh --hold >/dev/null && echo "snap refreshes held"; fi

  echo "== pin kernel $KVER"
  for _ in $(seq 1 120); do fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break; echo "waiting for dpkg lock"; sleep 5; done
  local held new=()
  held=$(apt-mark showhold)
  for p in $(kernel_pkgs); do grep -qx "$p" <<<"$held" || new+=("$p"); done
  if (( ${#new[@]} )); then apt-mark hold "${new[@]}"; printf '%s\n' "${new[@]}" >> "$HELD"; fi
  local entry; entry=$(grub_entry) || true
  [[ -n "$entry" ]] || { echo "no GRUB entry for $KVER found in /boot/grub/grub.cfg (run update-grub?)"; exit 1; }
  grub_set GRUB_DEFAULT "$entry"
  grub_set GRUB_RECORDFAIL_TIMEOUT 5
  update-grub 2>&1 | tail -2
  grep -qF "set default=\"$entry\"" /boot/grub/grub.cfg && echo "GRUB default: $entry" \
    || { echo "GRUB default not found in grub.cfg after update-grub"; exit 1; }

  echo "== reboot after a kernel panic"
  printf '# ojsdn campaign mode (install/prep-host.sh)\nkernel.panic = 10\n' > "$SYSCTL_CONF"
  sysctl -q -p "$SYSCTL_CONF"
  if [[ $HEADLESS -eq 1 ]]; then systemctl set-default multi-user.target; echo "boots to multi-user.target from now on"; fi
  echo; check || true
  echo; echo ">>> reboot once so the machine comes up exactly as it will during the campaign <<<"
}

undo() {
  [[ -f "$STATE" ]] || { echo "no $STATE: nothing to undo"; exit 1; }
  local kind u s
  rm -f "$APT_CONF" "$SYSCTL_CONF"; sysctl -q --system || true
  if [[ -s "$HELD" ]]; then xargs -a "$HELD" apt-mark unhold; fi
  # only the two keys apply changed (tune-host.sh may have edited the cmdline since)
  local k old
  for k in GRUB_DEFAULT GRUB_RECORDFAIL_TIMEOUT; do
    if old=$(grep "^$k=" "$STATE_DIR/grub.default.before" | tail -1); then sed -i "s|^$k=.*|$old|" /etc/default/grub
    else sed -i "/^$k=/d" /etc/default/grub; fi
  done
  update-grub 2>&1 | tail -1
  while read -r kind u s; do
    case "$kind" in
      unit)
        [[ "$s" == masked ]] && continue
        systemctl unmask "$u" >/dev/null 2>&1 || true
        if [[ "$s" == enabled ]]; then systemctl enable "$u" 2>/dev/null || true; [[ "$u" == *.timer ]] && systemctl start "$u" || true; fi ;;
      default_target) systemctl set-default "$u" ;;
      snap_hold) [[ "$u" == no ]] && command -v snap >/dev/null && snap refresh --unhold >/dev/null || true ;;
    esac
  done < <(grep -v '^#' "$STATE")
  local done_dir; done_dir="$STATE_DIR/undone-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$done_dir" && mv "$STATE" "$HELD" "$STATE_DIR/grub.default.before" "$done_dir/"
  echo "campaign mode removed (kernel.panic stays until reboot); reboot to boot the newest kernel again"
}

case "$MODE" in
  check) check ;;
  apply) apply ;;
  undo) undo ;;
esac
