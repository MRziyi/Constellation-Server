#!/usr/bin/env bash
# Build the glass debug APK, install it on the glasses, and reconnect.
# Compile-checks first (Kotlin) so a typo fails fast before the full assemble.
# NOTE: a reinstall RESETS the appops, which is why reconnect-glass.sh re-grants
# them right after — don't skip that step.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
set -e

cd "$GLASS_REPO"
echo "== compile (Kotlin) =="
./gradlew :app:compileGlassDebugKotlin
echo "== assemble debug APK =="
./gradlew :app:assembleGlassDebug

adb_has_device || { echo "✗ adb can't see $DEV — plug in / check USB"; exit 1; }
echo "== install (-r keeps pairing/cookie/slots) =="
adb -s "$DEV" install -r "$APK"

set +e
echo "== reconnect =="
"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reconnect-glass.sh"
