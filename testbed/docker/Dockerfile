# Emulation image for the ojsdn Wi-Fi campaign: the user space of an emulation host (what
# install/install-emu-host.sh builds natively), with pinned versions.
#
#   Mininet-WiFi 90c2d1d (+ headless TkAgg->Agg patch), its wifi deps / Mininet / OpenFlow / OVS
#   wmediumd: ramonfontes/wmediumd at commit 63e9fc9 + install/wmediumd_ojsdn.py (OJSDN_WMD_*), not util/install.sh's unpinned clone
#   hostapd + wpa_supplicant 2.11 (w1.fi, WNM/11r/11k) in /usr/local/sbin, first on PATH
#   iperf3 3.18 (--json-stream), irtt 0.9.0 (Ubuntu), Python venv /opt/venv (requirements.lock)
#
# The kernel side stays on the host: mac80211_hwsim and openvswitch are loaded from the host's
# /lib/modules (mounted read-only); see docker/ojsdn-docker.sh for the run options.
FROM ubuntu:24.04

ARG MNWIFI_COMMIT=90c2d1df56fe11aa49ef07cf86fd166e842e8761
ARG WMEDIUMD_COMMIT=63e9fc995cfb260065c7a0d4c2b14f715f224436
ARG HOSTAP_VER=2.11
ARG IPERF_VER=3.18
ENV DEBIAN_FRONTEND=noninteractive PIP_BREAK_SYSTEM_PACKAGES=1 LANG=C.UTF-8 \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# no init system in the image: keep package scripts from starting services at build time
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
 && apt-get update && apt-get install -y --no-install-recommends \
      sudo git ca-certificates wget curl jq build-essential pkg-config autoconf automake libtool help2man \
      python3 python3-dev python3-pip python3-venv python3-setuptools \
      iproute2 iputils-ping iw kmod procps psmisc ethtool socat tcpdump net-tools lsof \
      libnl-3-dev libnl-genl-3-dev libnl-route-3-dev libssl-dev libpcap-dev libevent-dev libconfig-dev \
      irtt binutils \
 && rm -rf /var/lib/apt/lists/*

# Mininet-WiFi at the pinned commit, patched for a headless host (as install-emu-host.sh does)
WORKDIR /opt/src
COPY patch-mnwifi.py /opt/src/
RUN git clone https://github.com/intrig-unicamp/mininet-wifi.git && cd mininet-wifi && git checkout -q ${MNWIFI_COMMIT} \
 && python3 /opt/src/patch-mnwifi.py mn_wifi/telemetry.py && grep -q "headless (ojsdn patch)" mn_wifi/telemetry.py
# -W wifi deps, -n Mininet(+Mininet-WiFi), -f OpenFlow, -v OVS; no -l (wmediumd comes from the pinned source below)
RUN cd mininet-wifi && apt-get update \
 && NEEDRESTART_MODE=a util/install.sh -Wnfv \
 && install -m 755 bin/mn /usr/local/bin/mn \
 && rm -rf /var/lib/apt/lists/*

# wmediumd at the pinned commit (GPL-2.0-or-later), patched by install/wmediumd_ojsdn.py
# (the same changes as install/wmediumd-ojsdn.patch)
COPY wmediumd_ojsdn.py /opt/src/
RUN git clone https://github.com/ramonfontes/wmediumd.git && cd wmediumd && git checkout -q ${WMEDIUMD_COMMIT} && cd .. \
 && python3 /opt/src/wmediumd_ojsdn.py wmediumd/wmediumd/wmediumd.c \
 && make -C wmediumd/wmediumd >/dev/null 2>&1 && install -m 755 wmediumd/wmediumd/wmediumd /usr/bin/wmediumd \
 && strings /usr/bin/wmediumd | grep -q OJSDN_WMD_MAXQ_US

# hostapd / wpa_supplicant 2.11 into /usr/local/sbin (ahead of Mininet-WiFi's 2.12-devel in /usr/local/bin)
RUN cd /tmp && wget -q https://w1.fi/releases/hostapd-${HOSTAP_VER}.tar.gz https://w1.fi/releases/wpa_supplicant-${HOSTAP_VER}.tar.gz \
 && tar xzf hostapd-${HOSTAP_VER}.tar.gz && tar xzf wpa_supplicant-${HOSTAP_VER}.tar.gz \
 && cd hostapd-${HOSTAP_VER}/hostapd && cp defconfig .config \
 && printf 'CONFIG_DRIVER_NL80211=y\nCONFIG_LIBNL32=y\nCONFIG_IEEE80211R=y\nCONFIG_IEEE80211N=y\nCONFIG_IEEE80211AC=y\nCONFIG_WNM=y\nCONFIG_INTERWORKING=y\nCONFIG_CTRL_IFACE=y\n' >> .config \
 && make -j"$(nproc)" >/dev/null && make install BINDIR=/usr/local/sbin >/dev/null \
 && cd /tmp/wpa_supplicant-${HOSTAP_VER}/wpa_supplicant && cp defconfig .config \
 && printf 'CONFIG_DRIVER_NL80211=y\nCONFIG_LIBNL32=y\nCONFIG_IEEE80211R=y\nCONFIG_WNM=y\nCONFIG_INTERWORKING=y\nCONFIG_CTRL_IFACE=y\nCONFIG_BGSCAN_SIMPLE=y\n' >> .config \
 && make -j"$(nproc)" >/dev/null && make install BINDIR=/usr/local/sbin >/dev/null \
 && cd / && rm -rf /tmp/hostapd-* /tmp/wpa_supplicant-* \
 && hostapd -v 2>&1 | grep -q "v${HOSTAP_VER}$" && wpa_supplicant -v 2>&1 | grep -q "v${HOSTAP_VER}$"

# iperf3 with --json-stream
RUN cd /tmp && wget -q https://github.com/esnet/iperf/releases/download/${IPERF_VER}/iperf-${IPERF_VER}.tar.gz \
 && tar xzf iperf-${IPERF_VER}.tar.gz && cd iperf-${IPERF_VER} && ./configure --prefix=/usr/local >/dev/null \
 && make -j"$(nproc)" >/dev/null && make install >/dev/null && ldconfig && cd / && rm -rf /tmp/iperf-* \
 && iperf3 --help | grep -q -- --json-stream

# Python venv outside the (bind-mounted) testbed folder; pinned versions (requirements.lock)
# PyYAML and psutil come from Ubuntu's packages (venv with --system-site-packages), the rest from the lock
RUN apt-get update && apt-get install -y --no-install-recommends python3-yaml python3-psutil && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /opt/requirements.lock
RUN python3 -m venv --system-site-packages /opt/venv \
 && /opt/venv/bin/pip install -q --upgrade pip && /opt/venv/bin/pip install -q -r /opt/requirements.lock \
 && SITE="$(/opt/venv/bin/python -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
 && echo "import mpl_toolkits" > "$SITE/00-ojsdn-mpl_toolkits.pth" \
 && /opt/venv/bin/python -c "import mn_wifi, pandas, zmq, ollama, jsonschema, yaml, psutil; print('venv OK')"

COPY entrypoint.sh /usr/local/bin/ojsdn-entrypoint
RUN chmod 755 /usr/local/bin/ojsdn-entrypoint
# SHELL: Mininet's Node.popen() runs commands with $SHELL -c (sudo sets it on a host, Docker does not)
ENV OJSDN_PYTHON=/opt/venv/bin/python OJSDN_IN_DOCKER=1 SHELL=/bin/bash
WORKDIR /ojsdn/testbed
ENTRYPOINT ["/usr/local/bin/ojsdn-entrypoint"]
CMD ["bash"]
