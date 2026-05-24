#!/usr/bin/env python3
"""End-to-end Phase 1 test harness — fake-Glass over WSS.

Usage:
  python test-harness/full_loop.py

Prerequisites:
  - cortex running on ws://127.0.0.1:8888
  - tool-agent running on ws://127.0.0.1:8889
  - Twin seed copied to ~/constellation/twin/

What it does:
  1. Connects to Cortex's Glass-facing endpoint.
  2. Sends a `user_invoke {text: "hello cortex"}`.
  3. Receives the echo `preview_action` command.
  4. Sends `user_decision {SEND}` referencing the cmd id.
  5. Asserts Twin has a new receipt + CHANGELOG entry.

Success criterion per IMPLEMENTATION-PLAN.md Phase 1:
  - Exits 0
  - ~/constellation/twin/receipts/{today}.md grew by one section
  - ~/constellation/twin/CHANGELOG.md grew by one entry
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import websockets


CORTEX_URL = os.environ.get("CORTEX_URL", "ws://127.0.0.1:8888")
TWIN_ROOT = Path.home() / "constellation" / "twin"


async def run() -> int:
    if not TWIN_ROOT.exists():
        print(f"FAIL: Twin not found at {TWIN_ROOT}", file=sys.stderr)
        print(f"      Run: cp -r {Path(__file__).resolve().parents[1] / 'twin-seed'} {TWIN_ROOT}", file=sys.stderr)
        return 1

    today_receipt = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    changelog = TWIN_ROOT / "CHANGELOG.md"

    receipt_size_before = today_receipt.stat().st_size if today_receipt.exists() else 0
    changelog_size_before = changelog.stat().st_size if changelog.exists() else 0

    async with websockets.connect(CORTEX_URL) as ws:
        invoke = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "user_invoke",
            "payload": {"text": "hello cortex"},
        }
        print(f">>> {json.dumps(invoke)}")
        await ws.send(json.dumps(invoke))

        # Expect a Command back (the preview_action card)
        cmd_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        cmd = json.loads(cmd_raw)
        print(f"<<< {cmd_raw[:200]}...")
        assert cmd["kind"] == "preview_action", f"expected preview_action, got {cmd['kind']}"
        cmd_id = cmd["id"]
        print(f"    received cmd {cmd_id}")

        decision = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "user_decision",
            "payload": {"in_reply_to": cmd_id, "decision": "send"},
        }
        print(f">>> {json.dumps(decision)}")
        await ws.send(json.dumps(decision))

    # Give Cortex a moment to flush receipts
    await asyncio.sleep(0.5)

    if not today_receipt.exists() or today_receipt.stat().st_size <= receipt_size_before:
        print(f"FAIL: receipt file did not grow ({today_receipt})", file=sys.stderr)
        return 2

    if not changelog.exists() or changelog.stat().st_size <= changelog_size_before:
        print(f"FAIL: CHANGELOG did not grow ({changelog})", file=sys.stderr)
        return 3

    print("PASS: receipt + CHANGELOG both grew")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
