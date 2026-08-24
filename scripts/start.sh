#!/usr/bin/env bash
# Bring up both services (tool-agent first, then cortex). Idempotent — safe to
# run whether they're loaded, idle, or already running. After a reboot with
# auto-login this is usually unnecessary (RunAtLoad starts them), but it's the
# one-shot "make sure everything's up" button.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

echo "0) Tailscale (cortex binds to its IP — after a reboot it's often down)…"
wait_tailscale 30 || echo "  ⚠ continuing, but cortex won't be reachable until Tailscale is up"

echo "1) load the launchd jobs…"
for p in "$TOOL_PLIST" "$CORTEX_PLIST"; do
  [ -f "$p" ] || { echo "  ✗ missing plist: $p"; continue; }
  if launchctl bootstrap "$GUI" "$p" 2>/dev/null; then
    echo "  bootstrapped $(basename "$p")"
  else
    echo "  $(basename "$p") already loaded"
  fi
done
# kickstart -k cortex so it REBINDS now Tailscale is up (handles the common case
# where it booted before Tailscale and its first bind failed). tool-agent just needs to run.
launchctl kickstart "$GUI/$TOOL_LABEL"      2>/dev/null || true
launchctl kickstart -k "$GUI/$CORTEX_LABEL" 2>/dev/null || true

echo "Waiting for cortex…"
wait_health 20
echo "  server_bound=$(field server_bound)  tool_conn=$(field tool_conn)"
echo "  (tool_conn turns True on the first tool dispatch — Cortex connects lazily)"
echo "  (server_bound needs the glasses awake + connected — run reconnect-glass.sh if False)"
