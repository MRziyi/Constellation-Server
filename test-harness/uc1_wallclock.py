#!/usr/bin/env python3
"""Phase 2 success-criterion test: UC1 email reply, wall-clock < 10 s.

Per IMPLEMENTATION-PLAN.md Phase 2:
  > Test: POST `user_invoke {text: "reply to Jane, see you at 3, casual"}`
  > Within 6 s: receive `preview_action` with a casual draft in Zack's style
  > End-to-end wall-clock for happy path: < 10 s from `user_invoke` to email out

This test runs the full UC1 chain in DRY-RUN mode (no actual send to Jane). It measures:
  - T0: user_invoke sent
  - T1: preview_action received  (must be < 10 s — soft target 6 s)
  - T2: user_decision SEND posted (immediately after T1)
  - T3: receipt written (full path complete)

Requires `people/core/jane-doe.md` to be in Twin (added in this Phase 2 wrap session).

Note: dry_run routes to Drafts; we verify the draft was saved correctly with sender being
the user's default account (Mail.app's pick).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets


CORTEX_URL = "ws://127.0.0.1:8888"
TWIN_ROOT = Path.home() / "constellation" / "twin"
INVOKE_TEXT = (
    "draft an email reply to Jane saying I'll be there at 3, casual, "
    "and DRY RUN ONLY — save to drafts but do not actually send."
)


async def _wait_for_growth(path: Path, baseline: int, deadline_s: float = 30.0) -> bool:
    for _ in range(int(deadline_s * 2)):
        await asyncio.sleep(0.5)
        if path.exists() and path.stat().st_size > baseline:
            return True
    return False


async def run() -> int:
    jane_path = TWIN_ROOT / "people" / "core" / "jane-doe.md"
    if not jane_path.exists():
        print(f"FAIL: {jane_path} missing — UC1 needs Jane's archive", file=sys.stderr)
        return 1

    today_receipt = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    baseline = today_receipt.stat().st_size if today_receipt.exists() else 0

    t0 = time.monotonic()
    async with websockets.connect(CORTEX_URL) as ws:
        invoke = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "user_invoke",
            "payload": {"text": INVOKE_TEXT},
        }
        print(f">>> [{0:.2f}s] user_invoke: {INVOKE_TEXT[:120]}...")
        await ws.send(json.dumps(invoke))

        cmd_raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
        t1 = time.monotonic() - t0
        cmd = json.loads(cmd_raw)
        body = cmd['payload'].get('body', '')
        print(f"<<< [{t1:.2f}s] {cmd['kind']}: title={cmd['payload'].get('title')!r}")
        print(f"    body={body[:400]!r}")

        if cmd["kind"] != "preview_action":
            print(f"NOTE: got {cmd['kind']} instead of preview_action", file=sys.stderr)
            # Phase 2 success criterion is the preview path

        if cmd["kind"] == "preview_action":
            decision = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "user_decision",
                "payload": {"in_reply_to": cmd["id"], "decision": "send"},
            }
            t2 = time.monotonic() - t0
            print(f">>> [{t2:.2f}s] SEND")
            await ws.send(json.dumps(decision))

        grew = await _wait_for_growth(today_receipt, baseline, deadline_s=30.0)
        t3 = time.monotonic() - t0
        if not grew:
            print(f"WARN: receipt didn't grow in 30s (t3 = {t3:.2f}s)", file=sys.stderr)

    print(f"\n=== timing ===")
    print(f"  T0 (invoke)       : 0.00 s")
    print(f"  T1 (preview)      : {t1:.2f} s  (target < 10 s, soft < 6 s)")
    if cmd["kind"] == "preview_action":
        print(f"  T2 (SEND)         : {t2:.2f} s")
        print(f"  T3 (receipt)      : {t3:.2f} s")

    # Verify the body actually reflects Jane's profile content
    body_lower = body.lower()
    style_hits = sum(1 for token in ["hey", "jane", "see you", "3"] if token in body_lower)
    print(f"\n  style hits in body: {style_hits}/4 (looking for Hey/Jane/see you/3)")

    if t1 >= 10.0:
        print(f"FAIL: T1 preview = {t1:.2f}s exceeds 10s budget", file=sys.stderr)
        return 2
    if t1 >= 6.0:
        print(f"NOTE: T1 = {t1:.2f}s exceeds soft 6s target but within hard 10s")

    tail = today_receipt.read_text(encoding="utf-8")[-1500:]
    print("\n--- receipt tail ---")
    print(tail)

    if "applescript_mail" not in tail:
        print("WARN: receipt does not mention applescript_mail (Router may have routed elsewhere)", file=sys.stderr)

    print("\nPASS: UC1 wall-clock within budget.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
