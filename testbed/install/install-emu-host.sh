#!/usr/bin/env bash
# =====================================================================================
# install-emu-host.sh — turn a FRESH Ubuntu 24.04 (or 22.04) x86-64 machine into the
# emulation host of this project: Mininet-WiFi + mac80211_hwsim + wmediumd, hostapd /
# wpa_supplicant with 802.11k/v/r (WNM), Open vSwitch, iperf3 >= 3.17 (--json-stream),
# irtt, tc, Ollama + local SLMs, the Python venv, and (optionally) kernel/system tuning.
#
# Run as a NORMAL user with sudo rights, from anywhere:
#     bash install/install-emu-host.sh [options]
# Options:
#     --mnwifi-commit SHA   check out this Mininet-WiFi commit (default: current master, recorded)
#     --no-ollama           do not install Ollama here: llm.host in testbed.yaml is a LAN server (as in the paper: emulation host -> GPU node)
#                           whose models are pulled through its HTTP API, or cloud-only
#     --no-models           install Ollama but do not pull models
#     --ollama-version V    install this Ollama release (e.g. 0.40.6, as on the GPU host); default latest
#     --tune                apply GRUB/sysctl/governor tuning (install/tune-host.sh); reboot afterwards
#     --force-hostap-build  always build hostapd/wpa_supplicant 2.11 from source
# Idempotent: re-running skips what is already installed. Log: /tmp/ojsdn-install-*.log
# Run install/prep-host.sh (campaign mode: no auto-updates, kernel pinned) BEFORE this script.
# Ollama is installed CPU-only from the release tarball: the upstream install.sh would install
# NVIDIA's cuda-drivers (DKMS) as soon as lspci shows any NVIDIA card, even an unsupported one.
# =====================================================================================
set -euo pipefail

TESTBED="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="/tmp/ojsdn-install-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

MNWIFI_COMMIT=""; WITH_OLLAMA=1; WITH_MODELS=1; TUNE=0; FORCE_HOSTAP=0; OLLAMA_VERSION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mnwifi-commit) MNWIFI_COMMIT="$2"; shift ;;
    --no-ollama) WITH_OLLAMA=0 ;;
    --no-models) WITH_MODELS=0 ;;
    --ollama-version) OLLAMA_VERSION="$2"; shift ;;
    --tune) TUNE=1 ;;
    --force-hostap-build) FORCE_HOSTAP=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 64 ;;
  esac
  shift
done

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARN: %s\033[0m\n' "$*"; }
# non-interactive apt that waits for a running dpkg instead of failing, and lets needrestart act alone
apt_get() { sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get -o DPkg::Lock::Timeout=600 "$@"; }
[[ $EUID -eq 0 ]] && { echo "run as a normal user with sudo rights (the venv must belong to you)"; exit 1; }
sudo -n true 2>/dev/null || sudo -v   # -v alone asks for a password unless every sudoers entry is NOPASSWD

# ------------------------------------------------------------------ 0. preflight
say "0. Preflight"
. /etc/os-release
echo "OS: $PRETTY_NAME, kernel $(uname -r), $(nproc) vCPU, $(free -g | awk '/Mem:/{print $2}') GB RAM"
[[ "$ID" == "ubuntu" ]] || warn "not Ubuntu — package names may differ"
[[ "$(uname -m)" == "x86_64" ]] || warn "not x86-64"
grep -q avx2 /proc/cpuinfo || warn "CPU without AVX2: local SLM inference will be slow"
[[ $(nproc) -ge 8 ]] || warn "fewer than 8 vCPU: adapt cores.* in testbed.yaml"
bash "$TESTBED/install/check-net.sh" || { echo "emulation subnet collides with the LAN (see above)"; exit 1; }
bash "$TESTBED/install/prep-host.sh" --check >/dev/null \
  || warn "campaign mode not (fully) applied: sudo bash install/prep-host.sh  (details: --check)"

# ------------------------------------------------------------------ 1. base packages
say "1. Base packages"
echo "wireshark-common wireshark-common/install-setuid boolean false" | sudo debconf-set-selections
apt_get update
apt_get install -y git build-essential python3-dev python3-venv python3-pip pkg-config \
  iw wireless-tools rfkill net-tools iproute2 iputils-ping zstd \
  tcpdump tshark openvswitch-switch linux-tools-common jq golang-go \
  curl wget ca-certificates libnl-3-dev libnl-genl-3-dev libnl-route-3-dev libssl-dev libpcap-dev \
  autoconf automake libtool help2man coreutils util-linux psmisc ethtool socat iperf3
# Kernel-specific packages one by one: a missing name must not abort the whole install. HWE kernels
# (e.g. 7.0 on 24.04) have no linux-modules-extra: mac80211_hwsim is in linux-modules there.
for p in "linux-modules-extra-$(uname -r)" "linux-tools-$(uname -r)"; do
  if dpkg-query -W -f='${db:Status-Abbrev}' "$p" 2>/dev/null | grep -qE '^[ih]i'; then echo "$p installed"
  elif apt-cache show "$p" >/dev/null 2>&1; then apt_get install -y "$p" || warn "$p failed"
  else echo "$p not in the archive (fine if mac80211_hwsim loads below)"; fi
done
sudo systemctl enable --now openvswitch-switch
# NetworkManager must leave the emulated radios/links alone (it would scan and reconfigure them)
if systemctl is-active -q NetworkManager; then
  sudo tee /etc/NetworkManager/conf.d/90-ojsdn-unmanaged.conf >/dev/null <<'NMCONF'
# Mininet-WiFi interfaces are not NetworkManager's business (install/install-emu-host.sh)
[keyfile]
unmanaged-devices=driver:mac80211_hwsim;interface-name:hwsim*;interface-name:wlan*;interface-name:*-wlan*;interface-name:*-eth*;interface-name:*-mp*;interface-name:ap*;interface-name:s1;interface-name:ovs-system
NMCONF
  sudo systemctl reload NetworkManager && echo "NetworkManager: emulation interfaces unmanaged"
fi
sudo modprobe mac80211_hwsim radios=2 && sudo rmmod mac80211_hwsim && echo "mac80211_hwsim OK" \
  || { echo "mac80211_hwsim not available for kernel $(uname -r) (install linux-modules-extra)"; exit 1; }

# ------------------------------------------------------------------ 2. Mininet-WiFi
say "2. Mininet-WiFi"
MNW="$HOME/mininet-wifi"
if [[ ! -d "$MNW/.git" ]]; then git clone https://github.com/intrig-unicamp/mininet-wifi.git "$MNW"; fi
cd "$MNW"
if [[ -n "$MNWIFI_COMMIT" ]]; then git fetch --all -q && git checkout -q "$MNWIFI_COMMIT"; fi
git rev-parse HEAD | tee "$HOME/mnwifi_commit.txt"
# mn_wifi/telemetry.py forces matplotlib's TkAgg at import time, which raises on a headless host
# (no $DISPLAY) and breaks both setup.py and every `import mn_wifi.net`: fall back to Agg.
if grep -q "^matplotlib.use('TkAgg')$" mn_wifi/telemetry.py; then
  # util/install.sh runs as root, so after a failed attempt the tree may be root-owned
  PYW=python3; [[ -w mn_wifi/telemetry.py ]] || PYW="sudo python3"
  $PYW - <<'PYFIX'
p = "mn_wifi/telemetry.py"
s = open(p).read().replace("matplotlib.use('TkAgg')\n", "try:\n    matplotlib.use('TkAgg')\nexcept ImportError:  # headless (ojsdn patch)\n    matplotlib.use('Agg')\n", 1)
open(p, "w").write(s)
PYFIX
fi
if grep -q "headless (ojsdn patch)" mn_wifi/telemetry.py; then
  echo "local patch: mn_wifi/telemetry.py TkAgg -> Agg fallback (headless)" | tee -a "$HOME/mnwifi_commit.txt"
fi
# -I: ignore the cwd, otherwise the source checkout itself satisfies the import
if ! sudo python3 -I -c "import mn_wifi" 2>/dev/null || ! command -v mn >/dev/null; then
  # Ubuntu 24.04 marks the system Python as externally managed (PEP 668); Mininet(-WiFi) installs
  # itself system-wide because it runs as root with the system interpreter.
  # util/install.sh is not re-runnable: it patches the hostap submodule and the mininet clone and
  # aborts if the patches are already applied. Undo what an earlier (failed) attempt left behind.
  # Only when hostap is its own checkout: on a fresh clone the submodule is not initialised yet and
  # `git -C hostap` would reset/clean the Mininet-WiFi tree itself (undoing the telemetry.py patch).
  if [[ -e hostap/.git ]]; then
    sudo git -c safe.directory='*' -C hostap reset -q --hard && sudo git -c safe.directory='*' -C hostap clean -qfdx
  fi
  sudo rm -rf mininet iw "$HOME/openflow"
  sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a PIP_BREAK_SYSTEM_PACKAGES=1 util/install.sh -Wlnfv
fi
sudo python3 -I -c "import mn_wifi; print('mn_wifi OK')"
# util/install.sh installs plain Mininet after Mininet-WiFi, so /usr/local/bin/mn ends up being Mininet's,
# whose `mn -c` leaves hostapd/wmediumd/hwsim behind. Mininet-WiFi's mn handles both.
sudo install -m 755 "$MNW/bin/mn" /usr/local/bin/mn
command -v mn >/dev/null || { echo "mn not found after install"; exit 1; }
# per-run hwsim netlink buffer for wmediumd (medium_rcvbuf_kb); the default 32 KiB drops frames
bash "$TESTBED/install/patch-wmediumd.sh"
cd "$TESTBED"

# ------------------------------------------------------------------ 3. hostapd / wpa_supplicant with WNM (802.11v)
say "3. hostapd / wpa_supplicant (802.11k/v/r)"
# The binaries the experiments use are the ones on sudo's PATH (/usr/local/sbin first). They must be
# exactly HOSTAP_VER on every emulation host: Mininet-WiFi's util/install.sh -W leaves a 2.12-devel
# build in /usr/local/bin, which also has BTM but would make results differ between hosts.
HOSTAP_VER=2.11
hostap_bin() { sudo sh -c "command -v $1"; }
hostap_ver_ok() { local b; b="$(hostap_bin "$1")" && [[ $({ "$b" -v 2>&1 || true; } | grep -c "v${HOSTAP_VER}\$") -gt 0 ]]; }
hostap_ok() {
  hostap_bin hostapd >/dev/null && hostap_bin wpa_supplicant >/dev/null && hostap_bin hostapd_cli >/dev/null \
    && hostap_ver_ok hostapd && hostap_ver_ok wpa_supplicant \
    && [[ $(strings "$(hostap_bin hostapd)" | grep -ci bss_tm_req) -gt 0 ]] \
    && [[ $(strings "$(hostap_bin wpa_supplicant)" | grep -ci wnm) -gt 0 ]]   # no grep -q: SIGPIPE + pipefail
}
if [[ $FORCE_HOSTAP -eq 1 ]] || ! hostap_ok; then
  echo "building hostapd + wpa_supplicant $HOSTAP_VER from the official w1.fi sources (WNM/11r/11k enabled)"
  B="$(mktemp -d)"; cd "$B"
  wget -q https://w1.fi/releases/hostapd-2.11.tar.gz https://w1.fi/releases/wpa_supplicant-2.11.tar.gz
  tar xzf hostapd-2.11.tar.gz && tar xzf wpa_supplicant-2.11.tar.gz
  cd hostapd-2.11/hostapd && cp defconfig .config
  cat >> .config <<'EOF'
CONFIG_DRIVER_NL80211=y
CONFIG_LIBNL32=y
CONFIG_IEEE80211R=y
CONFIG_IEEE80211N=y
CONFIG_IEEE80211AC=y
CONFIG_WNM=y
CONFIG_INTERWORKING=y
CONFIG_CTRL_IFACE=y
EOF
  make -j"$(nproc)" && sudo make install BINDIR=/usr/local/sbin
  cd "$B/wpa_supplicant-2.11/wpa_supplicant" && cp defconfig .config
  cat >> .config <<'EOF'
CONFIG_DRIVER_NL80211=y
CONFIG_LIBNL32=y
CONFIG_IEEE80211R=y
CONFIG_WNM=y
CONFIG_INTERWORKING=y
CONFIG_CTRL_IFACE=y
CONFIG_BGSCAN_SIMPLE=y
EOF
  make -j"$(nproc)" && sudo make install BINDIR=/usr/local/sbin
  cd "$TESTBED"; rm -rf "$B"
  hash -r
fi
for b in hostapd wpa_supplicant; do echo "$(hostap_bin $b): $({ "$(hostap_bin $b)" -v 2>&1 || true; } | grep -m1 -o 'v2[.0-9a-z-]*')"; done   # -v exits 1
hostap_ok && echo "hostapd/wpa_supplicant $HOSTAP_VER with BTM (bss_tm_req) + WNM" \
  || warn "hostapd/wpa_supplicant are not $HOSTAP_VER with WNM — rerun with --force-hostap-build"

# ------------------------------------------------------------------ 4. iperf3 >= 3.17 (--json-stream)
say "4. iperf3 with --json-stream"
if ! iperf3 --help 2>&1 | grep -q -- --json-stream; then
  B="$(mktemp -d)"; cd "$B"
  wget -q https://github.com/esnet/iperf/releases/download/3.18/iperf-3.18.tar.gz
  tar xzf iperf-3.18.tar.gz && cd iperf-3.18 && ./configure --prefix=/usr/local >/dev/null && make -j"$(nproc)" >/dev/null
  sudo make install >/dev/null && sudo ldconfig
  cd "$TESTBED"; rm -rf "$B"; hash -r
fi
{ iperf3 --version 2>&1 || true; } | head -1
iperf3 --help 2>&1 | grep -q -- --json-stream && echo "iperf3 --json-stream OK" || warn "iperf3 without --json-stream"

# ------------------------------------------------------------------ 5. irtt
say "5. irtt"
if ! command -v irtt >/dev/null; then
  apt_get install -y irtt || { GOBIN=/tmp/irttbin go install github.com/heistp/irtt/cmd/irtt@latest && sudo install -m755 /tmp/irttbin/irtt /usr/local/bin/irtt; }
fi
{ irtt version 2>&1 || true; } | head -1
# the Ubuntu package starts an irtt server on the LAN; the experiments run their own inside h1
sudo systemctl disable --now irtt.service 2>/dev/null || true

# ------------------------------------------------------------------ 6. Python venv
say "6. Python venv (testbed/.venv, --system-site-packages)"
if [[ ! -x "$TESTBED/.venv/bin/python" ]]; then python3 -m venv --system-site-packages "$TESTBED/.venv"; fi
"$TESTBED/.venv/bin/pip" install -q --upgrade pip
"$TESTBED/.venv/bin/pip" install -q -r "$TESTBED/requirements.txt"
# Ubuntu's matplotlib-3.6.3-nspkg.pth pre-imports the SYSTEM mpl_toolkits at interpreter start;
# mn_wifi then loads the system mplot3d (matplotlib 3.6 / numpy 1.x) next to the venv's matplotlib
# and fails. A venv .pth runs first and imports the venv copy; the system .pth only appends to it.
SITE="$("$TESTBED/.venv/bin/python" -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
echo "import mpl_toolkits" > "$SITE/00-ojsdn-mpl_toolkits.pth"
sudo "$TESTBED/.venv/bin/python" -c "import mn_wifi, pandas, zmq, ollama, jsonschema; print('venv OK (root can import mn_wifi + deps)')"

# ------------------------------------------------------------------ 7. Ollama + models
if [[ $WITH_OLLAMA -eq 1 ]]; then
  say "7. Ollama (pinned to cores.llm) and local SLMs"
  if ! command -v ollama >/dev/null || { [[ -n "$OLLAMA_VERSION" ]] && ! ollama --version 2>&1 | grep -qF "$OLLAMA_VERSION"; }; then
    # what https://ollama.com/install.sh does, minus the GPU-driver part
    U="https://ollama.com/download/ollama-linux-amd64"; V="${OLLAMA_VERSION:+?version=$OLLAMA_VERSION}"
    sudo systemctl stop ollama 2>/dev/null || true
    sudo rm -rf /usr/local/lib/ollama
    if curl -fsIL "$U.tar.zst$V" >/dev/null 2>&1; then curl -fL "$U.tar.zst$V" | zstd -d | sudo tar -xf - -C /usr/local
    else curl -fL "$U.tgz$V" | sudo tar -xzf - -C /usr/local; fi
    id ollama >/dev/null 2>&1 || sudo useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama
    sudo usermod -aG ollama "$USER"
    sudo tee /etc/systemd/system/ollama.service >/dev/null <<'UNIT'
[Unit]
Description=Ollama Service
After=network-online.target

[Service]
ExecStart=/usr/local/bin/ollama serve
User=ollama
Group=ollama
Restart=always
RestartSec=3
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

[Install]
WantedBy=default.target
UNIT
    hash -r
  fi
  ollama --version 2>&1 | tail -1
  LLM_CORES="$("$TESTBED/.venv/bin/python" -c 'import sys; sys.path.insert(0,"'"$TESTBED"'"); from common import config as C; print(C.load_base()["cores"]["llm"])')"
  sudo mkdir -p /etc/systemd/system/ollama.service.d
  sudo tee /etc/systemd/system/ollama.service.d/ojsdn.conf >/dev/null <<EOF
[Service]
CPUAffinity=${LLM_CORES//,/ }
Environment="OLLAMA_KEEP_ALIVE=-1" "OLLAMA_NUM_PARALLEL=1" "OLLAMA_MAX_LOADED_MODELS=2" "CUDA_VISIBLE_DEVICES=-1"
EOF
  sudo systemctl daemon-reload && sudo systemctl enable --now ollama && sudo systemctl restart ollama
  for _ in $(seq 1 30); do curl -sf localhost:11434/api/tags >/dev/null && break; sleep 1; done
  if [[ $WITH_MODELS -eq 1 ]]; then bash "$TESTBED/install/pull-models.sh"; fi
else
  LLM_HOST="$("$TESTBED/.venv/bin/python" -c 'import sys; sys.path.insert(0,"'"$TESTBED"'"); from common import config as C; print(C.load_base()["llm"]["host"])')"
  say "7. No local Ollama: models on llm.host = $LLM_HOST"
  if [[ $WITH_MODELS -eq 1 ]]; then bash "$TESTBED/install/pull-models.sh" --host "$LLM_HOST" \
    || warn "could not pull into $LLM_HOST (run install/pull-models.sh --host $LLM_HOST later)"; fi
fi

# ------------------------------------------------------------------ 8. tuning
if [[ $TUNE -eq 1 ]]; then say "8. Host tuning"; sudo bash "$TESTBED/install/tune-host.sh"; fi

# ------------------------------------------------------------------ 9. record versions
say "9. Record environment"
bash "$TESTBED/install/record-env.sh" || true

say "DONE. Next steps:"
cat <<EOF
  1. Check $TESTBED/testbed.yaml (cores, LLM backend, gpu_host, cloud budget).
  2. sudo -E $TESTBED/.venv/bin/python $TESTBED/common/smoke_test.py --n_ap 4 --n_sta 4
  3. $TESTBED/.venv/bin/python $TESTBED/experiment-1/slm_bench.py --calls 2
  4. Follow the README (start with: sudo -E ./run-campaign.sh)
  Install log: $LOG
EOF
if [[ $TUNE -eq 1 ]]; then echo "  >>> REBOOT now so that the GRUB C-state settings take effect <<<"; fi
