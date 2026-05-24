#!/usr/bin/env python3
"""Phase 5 UC2 — REAL Claude Code reverse-wake demo end-to-end.

Triggers an actual CC permission prompt via a Bash write, watches the watcher detect it,
verifies the tool_card lands on fake-Glass, sends allow_once, and confirms CC proceeded
(the target file gets written).

Flow:
  1. user_invoke: ask Cortex to start CC interactively in ~/Code/Projects/Constellation
     and tell it to write 'hello from cc' to /tmp/cc-real-reverse-wake-test.txt via Bash
  2. CC starts in tmux. Soon it tries `Bash` and hits permission_request.
  3. Watcher (1.5s poll) detects "Do you want to proceed?" pattern → pushes
     tool_reverse_wake event with options [Allow once, Always allow, Deny].
  4. Cortex receives, builds preview_action tool_card, pushes to Glass.
  5. fake-Glass receives, sends user_decision {decision: 'allow_once'}.
  6. Cortex dispatches claude_code.send_keys [Enter] → CC option 1 (Yes) selected.
  7. CC executes Bash, writes the file.
  8. After a beat, kill the CC session.
  9. Verify /tmp/cc-real-reverse-wake-test.txt exists and contains "hello from cc".

This is the real UC2 demo (vs reverse_wake_flow.py which used synthetic injection).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import websockets


CORTEX_URL = "ws://127.0.0.1:8888"
TWIN_ROOT = Path.home() / "constellation" / "twin"
TARGET_FILE = Path("/tmp/cc-real-reverse-wake-test.txt")
INVOKE_TEXT = (
    "start an interactive Claude Code tmux session in "
    "~/Code/Projects/Constellation and tell it: "
    "use the Bash tool to run: echo 'hello from cc' > /tmp/cc-real-reverse-wake-test.txt"
)


async def run() -> int:
    # Pre-clean target
    if TARGET_FILE.exists():
        TARGET_FILE.unlink()

    today_receipt = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    baseline = today_receipt.stat().st_size if today_receipt.exists() else 0

    session_id_observed: str | None = None
    wake_card_observed = False

    async with websockets.connect(CORTEX_URL) as ws:
        invoke = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "user_invoke",
            "payload": {"text": INVOKE_TEXT},
        }
        print(f">>> user_invoke: {INVOKE_TEXT[:140]}...")
        await ws.send(json.dumps(invoke))

        # We expect: (1) preview_action to start CC, (2) we SEND, (3) wake_card lands soon
        # after CC tries Bash. Loop reading messages with a generous timeout.
        deadline = asyncio.get_event_loop().time() + 120.0

        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                print("WARN: no message in 30s window", file=sys.stderr)
                break
            cmd = json.loads(raw)
            print(f"<<< {cmd['kind']}: {cmd['payload'].get('title')!r}")
            body = cmd['payload'].get('body', '')
            print(f"    body={body[:300]!r}")
            print(f"    options={cmd['payload'].get('options')}")

            # If it's a preview_action for starting CC, SEND
            title = (cmd['payload'].get('title') or "").lower()
            if cmd["kind"] == "preview_action" and "needs you" not in title:
                decision = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "kind": "user_decision",
                    "payload": {"in_reply_to": cmd["id"], "decision": "send"},
                }
                print(f">>> SEND for {cmd['id']}")
                await ws.send(json.dumps(decision))
                # capture session_id from body
                m = re.search(r"cc-[0-9a-f]{10}", body)
                if m:
                    session_id_observed = m.group(0)
                continue

            # If it's the reverse-wake card (CC asking permission)
            if "needs you" in title or "allow once" in [o.lower() for o in (cmd['payload'].get('options') or [])]:
                wake_card_observed = True
                grant = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "kind": "user_decision",
                    "payload": {"in_reply_to": cmd["id"], "decision": "allow_once"},
                }
                print(f">>> ALLOW ONCE for {cmd['id']}")
                await ws.send(json.dumps(grant))
                # After granting, give CC a few seconds to actually write the file
                await asyncio.sleep(8.0)
                break

        # Clean up the CC session
        if session_id_observed:
            print(f"\n>>> cleanup: kill {session_id_observed}")
            cleanup = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "user_invoke",
                "payload": {"text": f"kill claude_code tmux session {session_id_observed}"},
            }
            await ws.send(json.dumps(cleanup))
            try:
                cleanup_cmd_raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                cleanup_cmd = json.loads(cleanup_cmd_raw)
                if cleanup_cmd["kind"] == "preview_action":
                    await ws.send(json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "kind": "user_decision",
                        "payload": {"in_reply_to": cleanup_cmd["id"], "decision": "send"},
                    }))
                    await asyncio.sleep(2.0)
            except asyncio.TimeoutError:
                print("WARN: cleanup timed out", file=sys.stderr)

    # Verify
    print(f"\n=== summary ===")
    print(f"  session_id observed : {session_id_observed}")
    print(f"  wake_card observed  : {wake_card_observed}")
    print(f"  target file exists  : {TARGET_FILE.exists()}")
    if TARGET_FILE.exists():
        print(f"  target file content : {TARGET_FILE.read_text()!r}")

    if not wake_card_observed:
        print("\nFAIL: never saw a reverse-wake tool_card from CC", file=sys.stderr)
        return 2

    if not TARGET_FILE.exists():
        print("\nFAIL: target file not written — CC didn't proceed after grant", file=sys.stderr)
        return 3

    content = TARGET_FILE.read_text().strip()
    if "hello from cc" not in content:
        print(f"FAIL: target file unexpected content: {content!r}", file=sys.stderr)
        return 4

    # Cleanup target
    TARGET_FILE.unlink()

    print("\nPASS: real CC reverse-wake loop verified end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
