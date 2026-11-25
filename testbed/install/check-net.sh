#!/usr/bin/env bash
# Check that the emulated network does not collide with the LAN (no sudo needed):
#     bash install/check-net.sh
# Emulation subnet (OJSDN_EMU_NET, default 10.0.0.0/24): h1 = 10.0.0.100, sta_i = 10.0.0.i
# (common/traffic.py, common/config.py; Mininet-WiFi ipBase in common/topo.py). It lives in network
# namespaces only, but it must still not overlap
#   * the ranges reserved for the LAN (OJSDN_LAN_RANGES, space-separated CIDRs of your LAN; default none)
#   * any address or route already on this host (LAN NIC, docker0, libvirt, VPN, ...)
# Also warns about interfaces left in the root namespace by a crashed run (fix: sudo mn -c).
# Exit 1 on a conflict.
set -euo pipefail
export OJSDN_EMU_NET="${OJSDN_EMU_NET:-10.0.0.0/24}"
export OJSDN_LAN_RANGES="${OJSDN_LAN_RANGES:-}"
{ ip -j -4 addr; echo; ip -j -4 route; echo; ip -j link; } | python3 -I -c '
import ipaddress as ia, json, os, re, sys
addr, route, link = (json.loads(x) for x in sys.stdin.read().strip().split("\n\n"))
emu = ia.ip_network(os.environ["OJSDN_EMU_NET"])
lan = [ia.ip_network(n) for n in os.environ["OJSDN_LAN_RANGES"].split()]
bad = 0
def res(ok, msg):
    global bad
    bad += not ok
    print(("  OK   " if ok else "  NO   ") + msg)
lan_s = " ".join(map(str, lan))
print(f"emulation subnet {emu}; reserved LAN ranges {lan_s}")
for n in lan:
    res(not emu.overlaps(n), f"{emu} vs reserved {n}")
for a in addr:
    for i in a.get("addr_info", []):
        cidr = "%s/%s" % (i["local"], i["prefixlen"])
        net = ia.ip_interface(cidr).network
        if net.is_loopback:
            continue
        in_lan = any(net.subnet_of(n) for n in lan if n.version == net.version)
        res(not emu.overlaps(net), "%s vs %s %s" % (emu, a["ifname"], cidr)
            + ("" if in_lan else "  (outside the reserved LAN ranges)"))
for r in route:
    d = r.get("dst", "")
    if d == "default" or not d:
        continue
    net = ia.ip_network(d if "/" in d else d + "/32", strict=False)
    if emu.overlaps(net):
        dev = r.get("dev")
        res(False, f"route {d} dev {dev} overlaps {emu}")
left = [l["ifname"] for l in link if re.match(r"(hwsim\d|wlan\d|sta\d+-|ap\d+-|h\d+-eth|s\d+-eth|mp\d)", l["ifname"])]
if left:
    print("  WARN leftover emulation interfaces in the root namespace: " + " ".join(left) + "  ->  sudo mn -c")
print(f"  -> {bad} conflict(s)")
sys.exit(1 if bad else 0)
'
