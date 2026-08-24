#!/usr/bin/env bash
# Show the state of everything: launchd jobs, ports, cortex health, glasses.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

echo "== launchd jobs (col1=PID, col2=last-exit) =="
launchctl list | grep -E "constellation" || echo "  (none loaded)"

echo "== listening ports (8888 glass-WSS / 8889 tool / 8890 HTTP) =="
lsof -nP -iTCP:8888 -iTCP:8889 -iTCP:8890 -sTCP:LISTEN 2>/dev/null \
  | awk 'NR==1 || /LISTEN/ {printf "  %-12s pid=%-7s %s\n", $1, $2, $9}' \
  || echo "  (nothing listening)"

echo "== cortex bind =="
echo "  $(http_base)  (IP read from the plist)"

echo "== health =="
H=$(health_json)
if [ -n "$H" ]; then echo "$H" | python3 -m json.tool 2>/dev/null | sed 's/^/  /'
else echo "  ✗ cortex HTTP not responding at $(http_base)"; fi

echo "== glasses (adb) =="
if adb_has_device; then echo "  ✓ adb sees $DEV"; else echo "  ✗ adb does not see $DEV (USB unplugged?)"; fi
