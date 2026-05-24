#!/usr/bin/env python3
"""Phase 2 Slice C — claude_code reverse-wake full loop (UC2 wiring).

Uses the adapter's `__test_inject_wake__` action to synthesize a
`tool_reverse_wake { permission_request }` event from inside Tool Agent, then exercises
the entire path:

  Tool Agent (synthetic watcher)
    → pushes event to Cortex over the persistent tool-conn
    → Cortex's demux reader routes it to _handle_tool_reverse_wake
    → Cortex builds preview_action `tool_card` with options [Allow once, Deny]
    → ships to fake-Glass
    → fake-Glass sends user_decision {decision: 'allow_once'}
    → Cortex looks up wake_response_map, dispatches claude_code.send_keys(sid, 'y\\n')
    → receipt written

Test verification:
  - fake-Glass receives the tool_card with the right title + options
  - Receipt grows with a `reverse_wake_*` entry referencing send_keys

This validates the wiring without depending on CC's actual permission-prompt UI.
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
FAKE_SESSION_ID = "cc-fakeperm01"


async def _wait_for_growth(path: Path, baseline: int, deadline_s: float = 30.0) -> bool:
    for _ in range(int(deadline_s * 2)):
        await asyncio.sleep(0.5)
        if path.exists() and path.stat().st_size > baseline:
            return True
    return False


async def run() -> int:
    today_receipt = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    baseline = today_receipt.stat().st_size if today_receipt.exists() else 0

    # We need TWO things to happen on the Glass conn:
    #  1. Receive the tool_card (pushed unsolicited by Cortex after wake event)
    #  2. Send back our user_decision
    # So we keep the Glass WSS open, and use ONE user_invoke to trigger the injection.

    async with websockets.connect(CORTEX_URL) as ws:
        invoke = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "user_invoke",
            "payload": {
                "text": (
                    f"use the claude_code tool's __test_inject_wake__ action with "
                    f"session_id={FAKE_SESSION_ID} to synthesize a permission_request event "
                    f"(this is a test of the reverse-wake wiring)"
                ),
            },
        }
        print(">>> user_invoke (inject synthetic wake)")
        await ws.send(json.dumps(invoke))

        cmds: list[dict] = []
        # Collect up to 2 messages: (a) preview/hud_show for the inject dispatch,
        # then (b) the tool_card pushed unsolicited after the wake fires.
        for i in range(2):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                print(f"WARN: only got {len(cmds)} message(s) before timeout", file=sys.stderr)
                break
            cmds.append(json.loads(raw))
            print(f"<<< [{i}] kind={cmds[-1]['kind']} title={cmds[-1]['payload'].get('title')!r}")
            print(f"     body={cmds[-1]['payload'].get('body', '')[:200]!r}")
            print(f"     options={cmds[-1]['payload'].get('options')}")

        # If the first message was preview_action for the inject dispatch, SEND it
        if cmds and cmds[0]["kind"] == "preview_action":
            decision = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "user_decision",
                "payload": {"in_reply_to": cmds[0]["id"], "decision": "send"},
            }
            print(f">>> SEND for inject dispatch (cmd {cmds[0]['id']})")
            await ws.send(json.dumps(decision))

            # Wait for the wake push (cmds[1] if not already received)
            if len(cmds) < 2:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                    cmds.append(json.loads(raw))
                    print(f"<<< [1] (post-SEND) kind={cmds[-1]['kind']} title={cmds[-1]['payload'].get('title')!r}")
                    print(f"     body={cmds[-1]['payload'].get('body', '')[:200]!r}")
                    print(f"     options={cmds[-1]['payload'].get('options')}")
                except asyncio.TimeoutError:
                    print("FAIL: never received reverse-wake tool_card after SEND", file=sys.stderr)
                    return 2

        # Find the reverse-wake card (title matches `claude_code needs you`)
        wake_card = None
        for c in cmds:
            if "needs you" in (c["payload"].get("title") or "") or "Allow once" in (c["payload"].get("options") or []):
                wake_card = c
                break
        if not wake_card:
            print("FAIL: no reverse-wake tool_card observed", file=sys.stderr)
            return 3

        # Send user_decision allow_once → should trigger send_keys 'y\n'
        decision = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "user_decision",
            "payload": {"in_reply_to": wake_card["id"], "decision": "allow_once"},
        }
        print(f"\n>>> user_decision: allow_once (cmd {wake_card['id']})")
        await ws.send(json.dumps(decision))

        await asyncio.sleep(2.0)

    grew = await _wait_for_growth(today_receipt, baseline, deadline_s=30.0)
    if not grew:
        print(f"FAIL: receipt did not grow", file=sys.stderr)
        return 4

    tail = today_receipt.read_text(encoding="utf-8")[-3000:]
    print("\n--- receipt tail ---")
    print(tail)

    if "reverse_wake_permission_request" not in tail:
        print("FAIL: receipt missing reverse_wake_permission_request entry", file=sys.stderr)
        return 5
    if "send_keys" not in tail:
        print("FAIL: receipt missing send_keys follow-up entry", file=sys.stderr)
        return 6

    print("\nPASS: full reverse-wake loop wired (Tool Agent → Cortex → Glass → Cortex → Tool Agent).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
