#!/usr/bin/env python3
"""Phase 2 Slice C — applescript_mail adapter end-to-end test.

Two-step safety:
  STEP 1 (always runs): dry_run send → message lands in Mail Drafts, no actual send.
  STEP 2 (only with --send-for-real): real send to you@example.com (Zack's test recipient).

Usage:
  python test-harness/mail_flow.py                 # dry-run only
  python test-harness/mail_flow.py --send-for-real # actually send the email

Prereqs:
  - cortex + tool-agent running with applescript_mail enabled
  - Mail.app TCC permission for the cortex python binary
  - Mail.app default account configured (sender will be whichever)
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
RECIPIENT = "you@example.com"

DRY_INVOKE = (
    f"draft an email to {RECIPIENT} with subject 'Constellation dry-run' and a one-line body "
    f"saying this is the dry-run from Cortex Router. save it to Drafts, do NOT send."
)
SEND_INVOKE = (
    f"send a short test email to {RECIPIENT}. subject: 'Constellation send test'. "
    f"body: 'Hi Ziyi, this is a real send from Cortex Router via Mail.app. "
    f"If you got this, Phase 2 mail adapter works end-to-end.'"
)


async def _wait_for_growth(path: Path, baseline: int, deadline_s: float = 30.0) -> bool:
    for _ in range(int(deadline_s * 2)):
        await asyncio.sleep(0.5)
        if path.exists() and path.stat().st_size > baseline:
            return True
    return False


async def _run_flow(invoke_text: str, label: str) -> int:
    today_receipt = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    baseline = today_receipt.stat().st_size if today_receipt.exists() else 0

    async with websockets.connect(CORTEX_URL) as ws:
        invoke = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "user_invoke",
            "payload": {"text": invoke_text},
        }
        print(f"\n=== {label} ===")
        print(f">>> user_invoke: {invoke_text[:120]}...")
        await ws.send(json.dumps(invoke))

        v1_raw = await asyncio.wait_for(ws.recv(), timeout=45.0)
        v1 = json.loads(v1_raw)
        print(f"<<< {v1['kind']}: title={v1['payload'].get('title')!r}")
        print(f"    body={v1['payload'].get('body')[:300]!r}")
        print(f"    options={v1['payload'].get('options')}")

        if v1["kind"] == "preview_action":
            decision = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "user_decision",
                "payload": {"in_reply_to": v1["id"], "decision": "send"},
            }
            print(f">>> user_decision: SEND (cmd {v1['id']})")
            await ws.send(json.dumps(decision))
        elif v1["kind"] == "hud_show":
            print("    (hud_show — auto-executed; no SEND needed)")

        grew = await _wait_for_growth(today_receipt, baseline, deadline_s=45.0)
        if not grew:
            print("WARN: receipt did not grow in 45 s; TCC prompt may be pending.", file=sys.stderr)

    if not today_receipt.exists() or today_receipt.stat().st_size <= baseline:
        print(f"FAIL ({label}): receipt did not grow", file=sys.stderr)
        return 2

    tail = today_receipt.read_text(encoding="utf-8")[-1200:]
    print("\n--- receipt tail ---")
    print(tail)
    return 0


async def run(send_for_real: bool) -> int:
    rc = await _run_flow(DRY_INVOKE, "STEP 1 (dry_run via Drafts)")
    if rc != 0:
        return rc

    if not send_for_real:
        print("\nDry-run complete. Verify a draft to you@example.com appears in Mail.app Drafts.")
        print("To actually send, re-run with --send-for-real.")
        return 0

    print("\n--- Pausing 2 s before the real send ---")
    await asyncio.sleep(2.0)
    return await _run_flow(SEND_INVOKE, "STEP 2 (REAL SEND to you@example.com)")


if __name__ == "__main__":
    send_for_real = "--send-for-real" in sys.argv
    sys.exit(asyncio.run(run(send_for_real)))
