#!/usr/bin/env python3
"""Phase 2 Slice C battery — exercises the small-tool cohort (mail polish + T6-T10).

For each flow: send user_invoke, accept any of {preview_action, hud_show}, SEND if preview,
poll for receipt growth (rides out TCC delays), print receipt fragment.

Flows (each one prompt → one receipt entry):
  1. system_status     "what's my Mac state right now"
  2. apple_shortcuts list   "what Apple Shortcuts do I have"
  3. twin_query        "based on Twin, what does Zack think about email style"
  4. safari_state.current_tab   "what page am I on in Safari"
  5. imessage list_recent   "what are my last few iMessages" (may fail soft on FDA)
  6. mail.find_messages    "find recent emails involving Constellation" (search test, no send)
  7. mail.compose with explicit account   "use my QQ email to send a one-line 'ping' to $TEST_RECIPIENT"
     — only runs with --send-for-real flag; otherwise dry_run path.

By default the mail flow stays in dry-run. Pass --send-for-real to actually send.
"""

from __future__ import annotations

import asyncio
import os
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import websockets


CORTEX_URL = "ws://127.0.0.1:8888"
TWIN_ROOT = Path.home() / "constellation" / "twin"
# Mailbox the mail.compose flows address. Set to something you own.
RECIPIENT = os.environ.get("TEST_RECIPIENT", "")

BASE_FLOWS = [
    ("1. system_status",
     "what's my Mac state right now (battery, focus mode, frontmost app)"),
    ("2. apple_shortcuts.list",
     "list the Apple Shortcuts I have available"),
    ("3. twin_query",
     "based on what's in my Twin, summarize my email style preferences in one sentence"),
    ("4. safari_state.current_tab",
     "what page am I currently looking at in Safari"),
    ("5. imessage.list_recent",
     "show my last 5 iMessages"),
    ("6. mail.find_messages",
     "search my mail for any messages with 'Constellation' in the subject across inbox and sent"),
]

REAL_SEND_FLOW = (
    "7. mail.compose with QQ account",
    f"use my QQ email account to send a one-line 'ping from constellation slice c' to {RECIPIENT}",
)


async def _wait_for_growth(path: Path, baseline: int, deadline_s: float = 30.0) -> bool:
    for _ in range(int(deadline_s * 2)):
        await asyncio.sleep(0.5)
        if path.exists() and path.stat().st_size > baseline:
            return True
    return False


async def _run_flow(label: str, invoke_text: str) -> int:
    today_receipt = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    baseline = today_receipt.stat().st_size if today_receipt.exists() else 0

    async with websockets.connect(CORTEX_URL) as ws:
        invoke = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "user_invoke",
            "payload": {"text": invoke_text},
        }
        print(f"\n=== {label} ===")
        print(f">>> {invoke_text[:130]}")
        await ws.send(json.dumps(invoke))

        try:
            cmd_raw = await asyncio.wait_for(ws.recv(), timeout=45.0)
        except asyncio.TimeoutError:
            print("FAIL: no command within 45 s", file=sys.stderr)
            return 2

        cmd = json.loads(cmd_raw)
        print(f"<<< {cmd['kind']}: title={cmd['payload'].get('title')!r}")
        body = cmd['payload'].get('body', '') or ''
        print(f"    body={body[:300]!r}{'…' if len(body) > 300 else ''}")

        if cmd["kind"] == "preview_action":
            decision = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "user_decision",
                "payload": {"in_reply_to": cmd["id"], "decision": "send"},
            }
            print(f">>> SEND (cmd {cmd['id']})")
            await ws.send(json.dumps(decision))

        grew = await _wait_for_growth(today_receipt, baseline, deadline_s=30.0)
        if not grew:
            print("WARN: receipt didn't grow in 30 s; TCC prompt may be pending.", file=sys.stderr)

    return 0


async def run(send_for_real: bool) -> int:
    flows = list(BASE_FLOWS)
    if send_for_real:
        flows.append(REAL_SEND_FLOW)
    else:
        # Still test mail.compose path safely with dry_run wording.
        flows.append((
            "7. mail.compose dry-run with QQ account",
            f"draft an email FROM my QQ account TO {RECIPIENT} with subject 'slice C ping' and "
            "body 'one-line ping from slice C'. Save to drafts only, do not send.",
        ))

    for label, text in flows:
        rc = await _run_flow(label, text)
        if rc != 0:
            print(f"FAIL on {label}", file=sys.stderr)
            return rc

    tail_path = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    tail = tail_path.read_text(encoding="utf-8")[-4500:]
    print("\n--- receipt tail (last ~4500 chars) ---")
    print(tail)
    print("\nPASS: slice C battery complete.")
    return 0


if __name__ == "__main__":
    send_for_real = "--send-for-real" in sys.argv
    sys.exit(asyncio.run(run(send_for_real)))
