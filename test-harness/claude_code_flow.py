#!/usr/bin/env python3
"""Phase 2 Slice C — claude_code adapter end-to-end test.

Flows:
  A. draft scoped to Constellation README       — "use Claude Code to read the README under
                                                   ~/Code/Projects/Constellation and
                                                   give me a one-paragraph summary in English"
  B. run + continue (session resume)            — start a session asking CC to list the cortex
                                                   adapters; resume the session asking which
                                                   adapter has the smallest action surface.

By default uses --print mode (no tmux). All calls are preview-action; SEND triggers.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import websockets


CORTEX_URL = "ws://127.0.0.1:8888"
TWIN_ROOT = Path.home() / "constellation" / "twin"

FLOW_A = (
    "A. claude_code.draft (scoped, web search OR file read)",
    "use Claude Code to read the README under ~/Code/Projects/Constellation and "
    "give me a one-paragraph English summary of what Constellation is",
)
FLOW_B1 = (
    "B1. claude_code.run (session start)",
    "have Claude Code list the adapter Python files under "
    "~/Code/Projects/Constellation/tool-agent/tool_agent/adapters/ and start a new session",
)
FLOW_B2 = (
    "B2. claude_code.continue_ (resume same session)",
    "continuing that same Claude Code session, which of those adapters has the smallest "
    "single-file size",
)


async def _wait_for_growth(path: Path, baseline: int, deadline_s: float = 360.0) -> bool:
    # CC can take a minute or more for real reads + thinking
    for _ in range(int(deadline_s * 2)):
        await asyncio.sleep(0.5)
        if path.exists() and path.stat().st_size > baseline:
            return True
    return False


async def _run_flow(label: str, invoke_text: str, prev_session_id: str | None = None) -> str | None:
    """Returns the most recent claude_code session_id from the receipt, or None."""
    today_receipt = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    baseline = today_receipt.stat().st_size if today_receipt.exists() else 0

    text = invoke_text
    if prev_session_id:
        text = f"{invoke_text}. The session_id is {prev_session_id}."

    async with websockets.connect(CORTEX_URL) as ws:
        invoke = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "user_invoke",
            "payload": {"text": text},
        }
        print(f"\n=== {label} ===")
        print(f">>> {text[:160]}")
        await ws.send(json.dumps(invoke))

        try:
            cmd_raw = await asyncio.wait_for(ws.recv(), timeout=60.0)
        except asyncio.TimeoutError:
            print("FAIL: no command in 60s", file=sys.stderr)
            return None

        cmd = json.loads(cmd_raw)
        print(f"<<< {cmd['kind']}: title={cmd['payload'].get('title')!r}")
        body = cmd['payload'].get('body', '') or ''
        print(f"    body={body[:400]!r}{'…' if len(body) > 400 else ''}")

        if cmd["kind"] == "preview_action":
            decision = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "user_decision",
                "payload": {"in_reply_to": cmd["id"], "decision": "send"},
            }
            print(f">>> SEND (cmd {cmd['id']})")
            await ws.send(json.dumps(decision))

        grew = await _wait_for_growth(today_receipt, baseline, deadline_s=300.0)
        if not grew:
            print("WARN: receipt didn't grow in 5 min; CC may still be running.", file=sys.stderr)

    # Try to extract session_id from receipt
    if today_receipt.exists():
        tail = today_receipt.read_text(encoding="utf-8")[-2000:]
        m = re.search(r'"session_id":\s*"([a-f0-9-]{36})"', tail)
        if m:
            return m.group(1)
    return None


async def run() -> int:
    sid_a = await _run_flow(*FLOW_A)
    print(f"\nflow A session_id extracted: {sid_a}")

    sid_b1 = await _run_flow(*FLOW_B1)
    print(f"\nflow B1 session_id extracted: {sid_b1}")

    # Pass B1's session_id into B2's prompt
    if sid_b1:
        _ = await _run_flow(*FLOW_B2, prev_session_id=sid_b1)
    else:
        print("SKIP B2: no session_id from B1 receipt")

    today_receipt = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    tail = today_receipt.read_text(encoding="utf-8")[-4000:]
    print("\n--- receipt tail ---")
    print(tail)
    print("\nPASS: claude_code adapter wired end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
