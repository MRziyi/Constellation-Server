#!/usr/bin/env bash
# Tail a log. Default: cortex stdout.
#   ./logs.sh           # /tmp/cortex.out.log
#   ./logs.sh err       # /tmp/cortex.err.log
#   ./logs.sh tool      # tool-agent stdout
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

case "${1:-cortex}" in
  cortex) f=/tmp/cortex.out.log;;
  err)    f=/tmp/cortex.err.log;;
  tool)   f="$SERVER_REPO/tool-agent/logs/tool-agent.out.log";;
  *) echo "usage: logs.sh [cortex|err|tool]"; exit 1;;
esac
echo "tail -f $f  (Ctrl-C to stop)"
tail -n 40 -f "$f"
