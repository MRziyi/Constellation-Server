#!/usr/bin/env bash
# Reconnect the glasses to Cortex after a BT-PAN idle-drop (server_bound=False).
# Re-grants the FGS battery-exemption appops (a reinstall resets them), then
# force-stop → wake → launch → HOME, and polls until server_bound=True.
# Needs adb (USB). A light tap won't do it — this is the full cycle.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

adb_has_device || { echo "✗ adb can't see $DEV — plug in / check USB"; exit 1; }

echo "re-granting FGS-survival appops (no-op if already granted)…"
adb -s "$DEV" shell dumpsys deviceidle whitelist +$GLASS_PKG >/dev/null 2>&1
adb -s "$DEV" shell cmd appops set $GLASS_PKG RUN_ANY_IN_BACKGROUND allow >/dev/null 2>&1

echo "restarting glass app (force-stop → wake → launch → home)…"
adb -s "$DEV" shell am force-stop $GLASS_PKG
adb -s "$DEV" shell input keyevent KEYCODE_WAKEUP
adb -s "$DEV" shell monkey -p $GLASS_PKG -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
sleep 7
adb -s "$DEV" shell input keyevent KEYCODE_HOME

echo "polling server_bound…"
wait_bound 40
