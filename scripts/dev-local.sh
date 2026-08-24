#!/usr/bin/env bash
# Local-only demo bring-up — no Tailscale, no Linux edge, no glasses.
#
# The public edge (edge.example.com) is gone, so the deployed path
# (browser → edge → tailnet → Mac) is dead. This runs every tier on this Mac
# and wires the browser straight at it:
#
#   browser :5173 ──vite proxy──▶ edge :9100 ──▶ cortex :8890 HTTP
#                                             └─▶ cortex :8888 WSS (/ws/glass)
#                                                 cortex ──▶ tool-agent :8889
#
# Usage:  ./scripts/dev-local.sh          # bring everything up
#         ./scripts/dev-local.sh down     # stop what this script started
set -euo pipefail

SERVER_REPO="${SERVER_REPO:-$HOME/Code/Projects/Constellation-Server}"
CONSOLE_REPO="${CONSOLE_REPO:-$HOME/Code/Projects/Constellation-Console}"
BIND="${BIND:-0.0.0.0}"          # 0.0.0.0 so the glasses on the same Wi-Fi can also reach it
PASSWORD="${CONSOLE_PASSWORD:-demo}"
RUN="${TMPDIR:-/tmp}/constellation-dev"
mkdir -p "$RUN"

GUI="gui/$(id -u)"

down() {
  echo "→ stopping dev processes"
  for f in "$RUN"/*.pid; do
    [ -f "$f" ] || continue
    kill "$(cat "$f")" 2>/dev/null && echo "  killed $(basename "$f" .pid)"
    rm -f "$f"
  done
}
[ "${1:-}" = "down" ] && { down; exit 0; }

# ── 0. Get the launchd cortex out of the way ─────────────────────────────────
# Its plist hardcodes --host <mac-host> (Tailscale). Tailscale is stopped, so
# it can never bind and KeepAlive respawns it forever. Boot it out for the demo.
echo "0) booting out the launchd cortex (it binds a dead Tailscale IP)…"
launchctl bootout "$GUI/com.constellation.cortex" 2>/dev/null && echo "  ✓ booted out" || echo "  (not loaded)"

# ── 1. tool-agent (:8889) ────────────────────────────────────────────────────
echo "1) tool-agent on :8889…"
if lsof -nP -iTCP:8889 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  ✓ already listening (launchd job — leaving it alone)"
else
  ( cd "$SERVER_REPO/tool-agent" && \
    nohup ./.venv/bin/python -m tool_agent.main >"$RUN/tool-agent.log" 2>&1 & echo $! >"$RUN/tool-agent.pid" )
  echo "  started (log: $RUN/tool-agent.log)"
fi

# ── 2. cortex (:8888 WSS + :8890 HTTP) ───────────────────────────────────────
echo "2) cortex on $BIND:8888 (glass WSS) + :8890 (HTTP)…"
( cd "$SERVER_REPO/cortex" && \
  PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH" USE_SDK_AGENT=1 \
  nohup ./.venv/bin/python -m cortex.main --host "$BIND" --http-host "$BIND" \
      >"$RUN/cortex.log" 2>&1 & echo $! >"$RUN/cortex.pid" )
for i in $(seq 20); do
  curl -sS --max-time 2 http://127.0.0.1:8890/api/health 2>/dev/null | grep -q '"ok"' && { echo "  ✓ healthy (${i}s)"; break; }
  sleep 1
done

# ── 3. console edge (:9100) — replaces the destroyed Linux box ───────────────
echo "3) console edge on 127.0.0.1:9100…"
if [ ! -d "$CONSOLE_REPO/edge/.venv" ]; then
  echo "  creating venv (one-time, ~30s)…"
  ( cd "$CONSOLE_REPO/edge" && python3 -m venv .venv && ./.venv/bin/pip install -q --upgrade pip && ./.venv/bin/pip install -q -e . )
fi
( cd "$CONSOLE_REPO/edge" && \
  CONSOLE_PASSWORD="$PASSWORD" \
  EDGE_BIND_HOST=127.0.0.1 EDGE_BIND_PORT=9100 \
  CORTEX_HTTP_URL=http://127.0.0.1:8890 \
  CORTEX_WSS_URL=ws://127.0.0.1:8888 \
  EDGE_COOKIE_SECURE=0 \
  nohup ./.venv/bin/python -m console_edge.main >"$RUN/edge.log" 2>&1 & echo $! >"$RUN/edge.pid" )
sleep 2
curl -sS --max-time 3 http://127.0.0.1:9100/api/health >/dev/null 2>&1 && echo "  ✓ up" || echo "  ✗ check $RUN/edge.log"

# ── 4. web (:5173), vite proxying /api + /ws at the LOCAL edge ───────────────
echo "4) console web on http://localhost:5173…"
( cd "$CONSOLE_REPO/web" && \
  VITE_DEV_PROXY=http://127.0.0.1:9100 \
  nohup pnpm dev >"$RUN/web.log" 2>&1 & echo $! >"$RUN/web.pid" )
sleep 3

echo
echo "────────────────────────────────────────────────────────"
echo "  Console:  http://localhost:5173   password: $PASSWORD"
echo "  Logs:     $RUN/{cortex,tool-agent,edge,web}.log"
echo "  Stop:     ./scripts/dev-local.sh down"
echo "────────────────────────────────────────────────────────"
