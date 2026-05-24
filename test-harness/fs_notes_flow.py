#!/usr/bin/env python3
"""Phase 2 Slice C — fs + apple_notes adapter end-to-end test.

Three flows (each a separate Voice Invoke session):
  A. fs.read    — "what's in the Constellation README?" → Router dispatches fs.read on the
                  README, returns content as hud_show.
  B. fs.grep    — "find every TODO across Constellation" → Router dispatches fs.grep,
                  returns matches as hud_show.
  C. apple_notes.create — "drop a thought: phase 2 slice c is shaping up" → Router
                          dispatches apple_notes.create, note appears in Notes.app.

After: cleanup the created Note.

Prereqs:
  - cortex + tool-agent running with fs + apple_notes enabled
  - Notes.app TCC permission for cortex python (will prompt first run)
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

FLOWS = [
    ("A. fs.read README",
     "read ~/Code/Projects/Constellation/README.md and show me what it says"),
    ("B. fs.grep TODOs",
     "search for 'TODO' across all files under ~/Code/Projects/Constellation/cortex/"),
    ("C. apple_notes.create",
     "drop a thought into Notes titled 'phase 2 slice c is shaping up', body 'fs + notes adapters wired 2026-05-24'"),
]


async def _wait_for_growth(path: Path, baseline: int, deadline_s: float = 45.0) -> bool:
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
        print(f">>> {invoke_text[:140]}")
        await ws.send(json.dumps(invoke))

        cmd_raw = await asyncio.wait_for(ws.recv(), timeout=45.0)
        cmd = json.loads(cmd_raw)
        print(f"<<< {cmd['kind']}: title={cmd['payload'].get('title')!r}")
        body = cmd['payload'].get('body', '')
        print(f"    body={body[:280]!r}{'…' if len(body) > 280 else ''}")

        if cmd["kind"] == "preview_action":
            decision = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "user_decision",
                "payload": {"in_reply_to": cmd["id"], "decision": "send"},
            }
            print(f">>> SEND (cmd {cmd['id']})")
            await ws.send(json.dumps(decision))

        grew = await _wait_for_growth(today_receipt, baseline, deadline_s=45.0)
        if not grew:
            print("WARN: receipt didn't grow in 45 s.", file=sys.stderr)

    return 0 if today_receipt.stat().st_size > baseline else 2


async def _cleanup_note() -> None:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e",
        'tell application "Notes" to delete (every note whose name is "phase 2 slice c is shaping up")',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()


async def run() -> int:
    for label, text in FLOWS:
        rc = await _run_flow(label, text)
        if rc != 0:
            print(f"FAIL on {label}", file=sys.stderr)
            return rc

    print("\n--- cleanup ---")
    await _cleanup_note()

    tail = (TWIN_ROOT / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md").read_text(encoding="utf-8")[-2500:]
    print("\n--- receipt tail (last ~2500 chars) ---")
    print(tail)

    print("\nPASS: fs + apple_notes adapters wired end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
