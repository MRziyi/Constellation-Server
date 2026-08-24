#!/usr/bin/env bash
# Shared config + helpers for the Constellation ops scripts.
# Sourced by the others; not run directly. Override any var via the environment:
#   DEV=ABC123 ./reconnect-glass.sh        GLASS_REPO=/path ./build-glass.sh

SERVER_REPO="${SERVER_REPO:-$HOME/Code/Projects/Constellation-Server}"
GLASS_REPO="${GLASS_REPO:-$HOME/Code/Projects/Constellation-Glass}"

GUI="gui/$(id -u)"
CORTEX_LABEL="com.constellation.cortex"
TOOL_LABEL="com.constellation.tool-agent"
CORTEX_PLIST="$HOME/Library/LaunchAgents/$CORTEX_LABEL.plist"
TOOL_PLIST="$HOME/Library/LaunchAgents/$TOOL_LABEL.plist"

DEV="${DEV:-<glass-serial>}"   # glasses adb serial
APK="$GLASS_REPO/app/build/outputs/apk/glass/debug/app-glass-debug.apk"
GLASS_PKG="com.constellation.glass"

# The IP Cortex binds to = the --http-host (else --host) in its plist (the
# Tailscale IP). Read it from the plist so these scripts are PORTABLE across
# machines — no hardcoded <mac-host> that would break on the Mac Mini.
mac_ip() {
  local ip
  ip=$(python3 - "$CORTEX_PLIST" 2>/dev/null <<'PY'
import plistlib, sys
a = plistlib.load(open(sys.argv[1], "rb"))["ProgramArguments"]
for flag in ("--http-host", "--host"):
    if flag in a:
        print(a[a.index(flag) + 1]); break
PY
)
  echo "${ip:-127.0.0.1}"
}

http_base() { echo "http://$(mac_ip):8890"; }
health_json() { curl -sS --max-time 5 "$(http_base)/api/health" 2>/dev/null; }
# field <key>  → prints the value of a top-level health key (server_bound, tool_conn, …)
field() { health_json | python3 -c "import json,sys;print(json.load(sys.stdin).get('$1'))" 2>/dev/null; }

# wait_health [secs]  → block until cortex answers /api/health, up to N sec
wait_health() {
  local secs="${1:-15}" i=0
  while [ "$i" -lt "$secs" ]; do
    health_json | grep -q '"status": "ok"' && { echo "  ✓ cortex healthy (${i}s)"; return 0; }
    sleep 1; i=$((i + 1))
  done
  echo "  ✗ cortex not healthy after ${secs}s"; return 1
}

# wait_bound [secs]  → block until the glasses are connected (server_bound=True)
wait_bound() {
  local secs="${1:-40}" i=0
  while [ "$i" -lt "$secs" ]; do
    [ "$(field server_bound)" = "True" ] && { echo "  ✓ server_bound=True"; return 0; }
    sleep 2; i=$((i + 2))
  done
  echo "  ✗ server_bound still False after ${secs}s"; return 1
}

adb_has_device() { adb devices 2>/dev/null | grep -q "$DEV"; }

# Cortex binds to the Tailscale IP (from its plist). After a reboot Tailscale is
# often not up yet → cortex's bind fails and it never serves. Ensure it's up.
wait_tailscale() {
  local secs="${1:-30}" i=0 ip; ip="$(mac_ip)"
  case "$ip" in 127.*|"") return 0 ;; esac   # not a tailscale bind → nothing to wait for
  while [ "$i" -lt "$secs" ]; do
    if ifconfig 2>/dev/null | grep -qF "$ip"; then echo "  ✓ tailscale $ip up"; return 0; fi
    [ "$i" -eq 0 ] && { echo "  tailscale $ip down → launching the app…"; open -a Tailscale 2>/dev/null || true; }
    sleep 2; i=$((i + 2))
  done
  echo "  ✗ tailscale $ip not up after ${secs}s — cortex can't bind to it"; return 1
}
