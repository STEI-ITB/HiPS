#!/bin/sh
# wpa_cli action script. Invoked as: wpa_event.sh <ifname> <CONNECTED|DISCONNECTED>
# Appends: <epoch.ns>,<ifname>,<event>,bssid=<bssid> to $OJSDN_EVENTS (set by topo.py).
OUT="${OJSDN_EVENTS:-/tmp/ojsdn_events_raw.csv}"
TS="$(date +%s.%N)"
BSSID="$(wpa_cli -i "$1" status 2>/dev/null | grep '^bssid' | head -1)"
echo "${TS},$1,$2,${BSSID}" >> "$OUT"
