"""Headless fix for Mininet-WiFi (same as install-emu-host.sh): mn_wifi/telemetry.py forces matplotlib's
TkAgg at import time, which raises without a display and breaks setup.py and `import mn_wifi.net`."""
import sys

p = sys.argv[1] if len(sys.argv) > 1 else "mn_wifi/telemetry.py"
s = open(p).read()
if "headless (ojsdn patch)" in s:
    sys.exit(0)
old = "matplotlib.use('TkAgg')\n"
if old not in s:
    sys.exit(f"{p}: TkAgg line not found, patch by hand")
open(p, "w").write(s.replace(old, "try:\n    matplotlib.use('TkAgg')\nexcept ImportError:  # headless (ojsdn patch)\n"
                                  "    matplotlib.use('Agg')\n", 1))
print("patched", p)
