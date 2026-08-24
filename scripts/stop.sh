#!/usr/bin/env bash
# Stop both services (bootout). KeepAlive won't resurrect a booted-out job until
# the next login/load, so this is a real stop (use start.sh to bring them back).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

for entry in "cortex:$CORTEX_PLIST" "tool-agent:$TOOL_PLIST"; do
  name="${entry%%:*}"; plist="${entry#*:}"
  if launchctl bootout "$GUI" "$plist" 2>/dev/null; then
    echo "stopped $name"
  else
    echo "$name was not loaded"
  fi
done
