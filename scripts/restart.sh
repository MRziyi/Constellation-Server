#!/usr/bin/env bash
# Restart a service in place (picks up edited code; WorkingDirectory runs the
# current source). Default: cortex — the common case after editing server.py.
#   ./restart.sh            # cortex
#   ./restart.sh tool       # tool-agent
#   ./restart.sh all        # both
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

case "${1:-cortex}" in
  cortex) launchctl kickstart -k "$GUI/$CORTEX_LABEL"; echo "restarted cortex";;
  tool)   launchctl kickstart -k "$GUI/$TOOL_LABEL";   echo "restarted tool-agent";;
  all)    launchctl kickstart -k "$GUI/$TOOL_LABEL";
          launchctl kickstart -k "$GUI/$CORTEX_LABEL"; echo "restarted tool-agent + cortex";;
  *) echo "usage: restart.sh [cortex|tool|all]"; exit 1;;
esac
wait_health 20
