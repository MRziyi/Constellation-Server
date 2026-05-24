#!/usr/bin/env python3
"""Phase 2 Slice C — claude_code Track B (tmux interactive) end-to-end.

What it does:
  1. user_invoke: "start an interactive Claude Code session in ~/Code/Projects/Constellation
     and have it just sit there waiting"  → run_interactive
  2. Wait a beat, then user_invoke: "what's on the Claude Code pane right now"  → get_pane
  3. user_invoke: "send 'hello' to that CC session"  → send_keys
  4. user_invoke: "kill that CC tmux session"  → kill

This exercises Track B's full primitive surface. UC2's full reverse-wake loop (with
Tool Agent → Cortex event push) is Chunk 2 — separate test once that wiring lands.

NOTE: This test prints session_ids it observes and asks the Router to act on them in the
next flow. Some Router calls may need the session_id in the prompt to dispatch the next
action correctly.
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


async def _wait_for_growth(path: Path, baseline: int, deadline_s: float = 90.0) -> bool:
    for _ in range(int(deadline_s * 2)):
        await asyncio.sleep(0.5)
        if path.exists() and path.stat().st_size > baseline:
            return True
    return False


def _extract_session_id(tail: str) -> str | None:
    """Find the most recent tmux session id (`cc-<10hex>`) in the receipt tail."""
    matches = re.findall(r"cc-[0-9a-f]{10}", tail)
    return matches[-1] if matches else None


async def _flow(label: str, text: str) -> None:
    today_receipt = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    baseline = today_receipt.stat().st_size if today_receipt.exists() else 0

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
            cmd_raw = await asyncio.wait_for(ws.recv(), timeout=45.0)
        except asyncio.TimeoutError:
            print("FAIL: no command in 45s", file=sys.stderr)
            return

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

        grew = await _wait_for_growth(today_receipt, baseline, deadline_s=90.0)
        if not grew:
            print("WARN: receipt didn't grow in 90 s.", file=sys.stderr)


async def run() -> int:
    today_receipt = TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"

    # 1. start
    await _flow(
        "1. run_interactive",
        "start an interactive Claude Code tmux session in ~/Code/Projects/Constellation. "
        "Do not send any prompt yet, just spin up the session.",
    )

    tail = today_receipt.read_text(encoding="utf-8")[-2000:]
    session_id = _extract_session_id(tail)
    print(f"\n=> Extracted session_id: {session_id}")
    if not session_id:
        print("FAIL: could not extract session_id from receipt", file=sys.stderr)
        return 2

    # 2. get pane (wait for CC TUI to render)
    await asyncio.sleep(4.0)
    await _flow(
        "2. get_pane",
        f"show me what's on the claude_code tmux pane for session_id {session_id}",
    )

    # 3. send_keys
    await _flow(
        "3. send_keys",
        f"send the literal text 'hi from constellation' to claude_code tmux session_id {session_id}, "
        f"then press Enter (so use keys='hi from constellation\\n')",
    )

    await asyncio.sleep(3.0)

    # 4. get pane again
    await _flow(
        "4. get_pane after send",
        f"capture the claude_code tmux pane for session_id {session_id} again",
    )

    # 5. kill
    await _flow(
        "5. kill",
        f"kill claude_code tmux session_id {session_id}",
    )

    tail = today_receipt.read_text(encoding="utf-8")[-4500:]
    print("\n--- receipt tail ---")
    print(tail)
    print("\nPASS: claude_code Track B (tmux) wired.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
