#!/usr/bin/env bash
# Constellation Phase 1 installer.
#
# Sets up:
#   1. Twin seed at ~/constellation/twin/  (if missing)
#   2. Python venvs for cortex/ and tool-agent/ (pip install -e .)
#   3. launchd plists (templated + symlinked into ~/Library/LaunchAgents/)
#   4. Starts both daemons
#
# Manual prereqs (you must do these first):
#   - Python 3.11+
#   - openai + anthropic + Claude Code CLI installed (for Phase 2+)
#   - Tailscale installed
#
# Usage:
#   ./scripts/install.sh                  # full install
#   ./scripts/install.sh --twin-only      # just copy twin-seed
#   ./scripts/install.sh --skip-launchd   # install code, skip daemonising

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TWIN_DEST="${HOME}/constellation/twin"

echo "Constellation Phase 1 installer"
echo "  Repo root:  ${REPO_ROOT}"
echo "  Twin dest:  ${TWIN_DEST}"
echo

# 1. Twin seed
if [ ! -d "${TWIN_DEST}" ]; then
    echo "→ Copying twin-seed/ to ${TWIN_DEST}"
    mkdir -p "$(dirname "${TWIN_DEST}")"
    cp -r "${REPO_ROOT}/twin-seed" "${TWIN_DEST}"
else
    echo "→ Twin already exists at ${TWIN_DEST}; leaving untouched"
fi

if [ "${1:-}" == "--twin-only" ]; then
    echo "Done (twin-only mode)."
    exit 0
fi

# 2. Python venvs + editable installs
for module in cortex tool-agent; do
    cd "${REPO_ROOT}/${module}"
    if [ ! -d ".venv" ]; then
        echo "→ Creating venv for ${module}/"
        python3 -m venv .venv
    fi
    echo "→ pip install -e ${module}/"
    ./.venv/bin/pip install --quiet --upgrade pip
    ./.venv/bin/pip install --quiet -e .
done
cd "${REPO_ROOT}"

# Logs dirs
mkdir -p "${REPO_ROOT}/cortex/logs"
mkdir -p "${REPO_ROOT}/tool-agent/logs"

if [ "${1:-}" == "--skip-launchd" ]; then
    echo
    echo "Done (skipped launchd)."
    echo "Run manually:"
    echo "  (term1) ${REPO_ROOT}/tool-agent/.venv/bin/python -m tool_agent.main"
    echo "  (term2) ${REPO_ROOT}/cortex/.venv/bin/python -m cortex.main"
    echo "  (term3) ${REPO_ROOT}/cortex/.venv/bin/python ${REPO_ROOT}/test-harness/full_loop.py"
    exit 0
fi

# 3. launchd plists (templated)
LAUNCHAGENTS="${HOME}/Library/LaunchAgents"
mkdir -p "${LAUNCHAGENTS}"

for module in cortex tool-agent; do
    plist_src="${REPO_ROOT}/${module}/launchd"
    plist_name=$(ls "${plist_src}"/*.plist | head -1)
    plist_basename=$(basename "${plist_name}")
    plist_dst="${LAUNCHAGENTS}/${plist_basename}"

    # Template substitution
    module_dir="${REPO_ROOT}/${module}"
    sed "s|__CORTEX_DIR__|${REPO_ROOT}/cortex|g;
         s|__TOOL_AGENT_DIR__|${REPO_ROOT}/tool-agent|g" \
        "${plist_name}" > "${plist_dst}"

    echo "→ Installed ${plist_dst}"
done

# 4. Load
for label in com.constellation.tool-agent com.constellation.cortex; do
    launchctl unload "${LAUNCHAGENTS}/${label}.plist" 2>/dev/null || true
    launchctl load   "${LAUNCHAGENTS}/${label}.plist"
    echo "→ launchctl loaded ${label}"
done

echo
echo "Done. Verify:"
echo "  launchctl list | grep constellation"
echo "  python3 test-harness/full_loop.py"
