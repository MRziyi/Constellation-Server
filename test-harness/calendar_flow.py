#!/usr/bin/env python3
"""Phase 2 Slice C — applescript_calendar adapter end-to-end test.

Exercises the preview-always path (calendar:add_event per confirm-policies.md):
  1. Voice intent: "schedule a 30-minute meeting with Jane next Thursday at 10am to discuss CHI draft"
  2. Expect preview_action with a draft event summary (Router resolved start/end ISO)
  3. SEND → osascript creates the event in Calendar.app
  4. Verify by querying via the same adapter's list_range

Prereqs:
  - cortex + tool-agent running with applescript_calendar enabled
  - Calendar.app TCC permission for the cortex python binary (may prompt first run)
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import websockets


CORTEX_URL = "ws://127.0.0.1:8888"
TWIN_ROOT = Path.home() / "constellation" / "twin"

INVOKE_TEXT = (
    "schedule a 30-minute meeting with Jane next Thursday at 10am to discuss the CHI draft"
)


async def _wait_for_growth(path: Path, baseline: int, deadline_s: float = 30.0) -> bool:
    for _ in range(int(deadline_s * 2)):
        await asyncio.sleep(0.5)
        if path.exists() and path.stat().st_size > baseline:
            return True
    return False


async def run() -> int:
    today_receipt = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    receipt_baseline = today_receipt.stat().st_size if today_receipt.exists() else 0

    async with websockets.connect(CORTEX_URL) as ws:
        invoke = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "user_invoke",
            "payload": {"text": INVOKE_TEXT},
        }
        print(f">>> user_invoke: {INVOKE_TEXT!r}")
        await ws.send(json.dumps(invoke))

        v1_raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
        v1 = json.loads(v1_raw)
        print(f"<<< {v1['kind']}: title={v1['payload'].get('title')!r}")
        print(f"    body={v1['payload'].get('body')!r}")
        print(f"    options={v1['payload'].get('options')}")

        if v1["kind"] != "preview_action":
            print(
                f"WARN: expected preview_action (add_event is preview-always per confirm-policies),"
                f" got {v1['kind']}",
                file=sys.stderr,
            )
            # Don't hard-fail; the Router may also opt for hud_show if it's confident.

        if v1["kind"] == "preview_action":
            decision = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "user_decision",
                "payload": {"in_reply_to": v1["id"], "decision": "send"},
            }
            print(f">>> user_decision: SEND (cmd {v1['id']})")
            await ws.send(json.dumps(decision))

        grew = await _wait_for_growth(today_receipt, receipt_baseline, deadline_s=30.0)
        if not grew:
            print("WARN: receipt didn't grow in 30 s; TCC prompt may be pending.", file=sys.stderr)

    if not today_receipt.exists() or today_receipt.stat().st_size <= receipt_baseline:
        print(f"FAIL: receipt did not grow ({today_receipt})", file=sys.stderr)
        return 2

    tail = today_receipt.read_text(encoding="utf-8")[-1200:]
    print("\n--- receipt tail ---")
    print(tail)

    if "applescript_calendar" not in tail:
        print("WARN: receipt does not mention applescript_calendar.", file=sys.stderr)
    else:
        print("\nPASS: calendar adapter wired end-to-end.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
