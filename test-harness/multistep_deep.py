#!/usr/bin/env python3
"""Phase 2 R-3 — multi-step task paradigm deep test suite.

Three scenarios covering the multi-step + free-form feedback paradigm:

  SCENARIO 1 (default-path advance):
    "Multi-step: read identity.md and tell me my name, then add a reminder
     for tomorrow 10am titled with that name."
    Expects: HUD-1 shows name → SEND → HUD-2 shows reminder preview → SEND →
             reminder created.

  SCENARIO 2 (free-form correction):
    Same as 1, but at HUD-2 user FEEDBACKs "actually make it 4pm not 10am"
    Expects: HUD-3 re-issues reminder preview with corrected time → SEND → created.

  SCENARIO 3 (free-form skip):
    "Multi-step: list my Constellation-related mail subjects, summarize them in one
     sentence, then add a reminder to follow up tomorrow afternoon."
    At HUD-1, user FEEDBACKs "looks good but skip the reminder step entirely"
    Expects: HUD-2 is a hud_show acknowledging skip, or Router ends task; no reminder
             created.

Each scenario opens its own WSS, runs through, verifies receipts + side effects.

The Router is a real GPT-5.4 call so exact behavior varies; tests assert structural
properties (HUD count, presence of receipts, side-effect verification) rather than
exact strings.
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


async def _send_recv(ws, payload: dict, timeout: float = 60.0) -> dict | None:
    await ws.send(json.dumps(payload))
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        return json.loads(raw)
    except asyncio.TimeoutError:
        return None


def _print_card(label: str, cmd: dict) -> None:
    print(f"<<< [{label}] kind={cmd['kind']} title={cmd['payload'].get('title')!r}")
    body = cmd['payload'].get('body', '') or ''
    print(f"     body={body[:400]!r}{'…' if len(body) > 400 else ''}")
    print(f"     options={cmd['payload'].get('options')}")


def _list_reminders_named(name_substr: str) -> list[str]:
    """Helper: list reminders in default list with name containing substring."""
    import subprocess
    r = subprocess.run(
        ["osascript", "-e",
         f'tell application "Reminders" to return name of (every reminder of list "Reminders" whose name contains "{name_substr}")'],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        return []
    return [n.strip() for n in r.stdout.strip().split(",") if n.strip()]


def _delete_reminders_named(name_substr: str) -> None:
    import subprocess
    subprocess.run(
        ["osascript", "-e",
         f'tell application "Reminders" to delete (every reminder of list "Reminders" whose name contains "{name_substr}")'],
        capture_output=True, text=True, timeout=60,
    )


async def scenario_1_default_advance() -> dict:
    """Two-step with default ring-tap advancement."""
    print("\n" + "=" * 70)
    print("SCENARIO 1 — multi-step default-path advance (read identity → add reminder)")
    print("=" * 70)

    invoke_text = (
        "do a multi-step task: first read my identity.md to extract my display name, "
        "show me the name on a card so I can confirm it, then on the second step add "
        "a reminder titled 'multistep-test <my_name>' due tomorrow 10am. "
        "Use task_continues=true on the first step."
    )
    name_marker = "multistep-test"
    _delete_reminders_named(name_marker)  # pre-clean

    cards: list[dict] = []
    async with websockets.connect(CORTEX_URL) as ws:
        invoke = {"ts": datetime.now(timezone.utc).isoformat(), "kind": "user_invoke",
                  "payload": {"text": invoke_text}}
        print(f">>> user_invoke: {invoke_text[:140]}...")
        await ws.send(json.dumps(invoke))

        # Round-1 card
        try:
            r1 = await asyncio.wait_for(ws.recv(), timeout=45.0)
        except asyncio.TimeoutError:
            return {"scenario": 1, "pass": False, "reason": "no R1 card"}
        c1 = json.loads(r1); cards.append(c1); _print_card("R1", c1)

        # SEND for round-1
        if c1["kind"] == "preview_action":
            print(">>> SEND R1")
            await ws.send(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                       "kind": "user_decision",
                                       "payload": {"in_reply_to": c1["id"], "decision": "send"}}))

        # Round-2 card
        try:
            r2 = await asyncio.wait_for(ws.recv(), timeout=45.0)
        except asyncio.TimeoutError:
            return {"scenario": 1, "pass": False, "reason": "no R2 card (R1 may have been task_continues=false)", "cards": cards}
        c2 = json.loads(r2); cards.append(c2); _print_card("R2", c2)

        # SEND for round-2 (final)
        if c2["kind"] == "preview_action":
            print(">>> SEND R2")
            await ws.send(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                       "kind": "user_decision",
                                       "payload": {"in_reply_to": c2["id"], "decision": "send"}}))
            await asyncio.sleep(3.0)

    await asyncio.sleep(2.0)
    found = _list_reminders_named(name_marker)
    print(f"\n  reminders matching '{name_marker}': {found}")
    _delete_reminders_named(name_marker)  # cleanup

    ok = len(cards) >= 2 and len(found) >= 1
    return {"scenario": 1, "pass": ok, "n_cards": len(cards), "reminders_created": len(found)}


async def scenario_2_correction() -> dict:
    """Same shape as scenario 1, but FEEDBACK at R2 to correct time."""
    print("\n" + "=" * 70)
    print("SCENARIO 2 — multi-step with free-form CORRECTION at intermediate step")
    print("=" * 70)

    invoke_text = (
        "multi-step task: step 1 = look at people/core/jane-doe.md and tell me her "
        "affiliation. step 2 = after I confirm, add a reminder titled "
        "'multistep-corr meeting Jane' for tomorrow 10am. "
        "Use task_continues=true between steps."
    )
    name_marker = "multistep-corr"
    _delete_reminders_named(name_marker)

    cards: list[dict] = []
    async with websockets.connect(CORTEX_URL) as ws:
        invoke = {"ts": datetime.now(timezone.utc).isoformat(), "kind": "user_invoke",
                  "payload": {"text": invoke_text}}
        print(f">>> user_invoke: {invoke_text[:140]}...")
        await ws.send(json.dumps(invoke))

        try:
            r1 = await asyncio.wait_for(ws.recv(), timeout=45.0)
        except asyncio.TimeoutError:
            return {"scenario": 2, "pass": False, "reason": "no R1"}
        c1 = json.loads(r1); cards.append(c1); _print_card("R1", c1)

        # SEND R1 → expect R2 with reminder preview at 10am
        print(">>> SEND R1")
        await ws.send(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                   "kind": "user_decision",
                                   "payload": {"in_reply_to": c1["id"], "decision": "send"}}))

        try:
            r2 = await asyncio.wait_for(ws.recv(), timeout=45.0)
        except asyncio.TimeoutError:
            return {"scenario": 2, "pass": False, "reason": "no R2", "cards": cards}
        c2 = json.loads(r2); cards.append(c2); _print_card("R2", c2)

        # FEEDBACK on R2 to correct time → expect R3 with 4pm reminder
        print(">>> FEEDBACK R2: 'actually make it 4pm not 10am'")
        await ws.send(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                   "kind": "user_decision",
                                   "payload": {"in_reply_to": c2["id"], "decision": "feedback",
                                               "feedback_text": "actually make it 4pm not 10am"}}))

        try:
            r3 = await asyncio.wait_for(ws.recv(), timeout=45.0)
        except asyncio.TimeoutError:
            return {"scenario": 2, "pass": False, "reason": "no R3 after correction", "cards": cards}
        c3 = json.loads(r3); cards.append(c3); _print_card("R3 (post-correction)", c3)

        # SEND R3 → create reminder
        if c3["kind"] == "preview_action":
            print(">>> SEND R3")
            await ws.send(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                       "kind": "user_decision",
                                       "payload": {"in_reply_to": c3["id"], "decision": "send"}}))
            await asyncio.sleep(3.0)

    await asyncio.sleep(2.0)
    found = _list_reminders_named(name_marker)
    print(f"\n  reminders matching '{name_marker}': {found}")
    _delete_reminders_named(name_marker)

    # Verify the body of R3 mentions 4pm (or 16:00)
    r3_body = cards[-1]['payload'].get('body', '') if cards else ''
    has_4pm_signal = any(s in r3_body.lower() for s in ["4pm", "4:00 pm", "16:00", "4 pm"])
    ok = len(cards) >= 3 and len(found) >= 1 and has_4pm_signal
    return {
        "scenario": 2, "pass": ok, "n_cards": len(cards),
        "reminders_created": len(found),
        "correction_reflected_in_r3": has_4pm_signal,
    }


async def scenario_3_skip() -> dict:
    """Multi-step where user FEEDBACK at R1 to skip the next step entirely."""
    print("\n" + "=" * 70)
    print("SCENARIO 3 — multi-step with free-form SKIP at intermediate step")
    print("=" * 70)

    invoke_text = (
        "multi-step: step 1 = read my identity.md and show me my role and operating "
        "philosophy in 2-3 sentences. step 2 = then add a reminder titled "
        "'multistep-skip never' due tomorrow. use task_continues=true between steps."
    )
    name_marker = "multistep-skip"
    _delete_reminders_named(name_marker)

    cards: list[dict] = []
    async with websockets.connect(CORTEX_URL) as ws:
        invoke = {"ts": datetime.now(timezone.utc).isoformat(), "kind": "user_invoke",
                  "payload": {"text": invoke_text}}
        print(f">>> user_invoke: {invoke_text[:140]}...")
        await ws.send(json.dumps(invoke))

        try:
            r1 = await asyncio.wait_for(ws.recv(), timeout=45.0)
        except asyncio.TimeoutError:
            return {"scenario": 3, "pass": False, "reason": "no R1"}
        c1 = json.loads(r1); cards.append(c1); _print_card("R1", c1)

        # FEEDBACK R1 to skip the reminder step
        skip_text = "thanks, that's all I needed — skip the reminder step, no need to create it"
        print(f">>> FEEDBACK R1: '{skip_text}'")
        await ws.send(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                   "kind": "user_decision",
                                   "payload": {"in_reply_to": c1["id"], "decision": "feedback",
                                               "feedback_text": skip_text}}))

        try:
            r2 = await asyncio.wait_for(ws.recv(), timeout=45.0)
        except asyncio.TimeoutError:
            return {"scenario": 3, "pass": False, "reason": "no R2 after skip feedback", "cards": cards}
        c2 = json.loads(r2); cards.append(c2); _print_card("R2 (post-skip)", c2)

        # If R2 is preview_action and creates the reminder, the Router didn't honor skip.
        # Acceptable R2 shapes: hud_show (acknowledging end), preview_action with no reminder action.
        # If preview_action, send the default; if hud_show, we're done.
        if c2["kind"] == "preview_action":
            print(">>> SEND R2 (if Router still wants confirmation, accept)")
            await ws.send(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                       "kind": "user_decision",
                                       "payload": {"in_reply_to": c2["id"], "decision": "send"}}))
            await asyncio.sleep(2.0)

    await asyncio.sleep(2.0)
    found = _list_reminders_named(name_marker)
    print(f"\n  reminders matching '{name_marker}' (expect ZERO since user skipped): {found}")
    _delete_reminders_named(name_marker)

    # Pass if NO reminder was created despite the prompt mentioning one
    ok = len(found) == 0
    return {"scenario": 3, "pass": ok, "n_cards": len(cards), "reminders_created": len(found)}


async def run() -> int:
    results = []
    for s in (scenario_1_default_advance, scenario_2_correction, scenario_3_skip):
        try:
            r = await s()
        except Exception as e:
            r = {"scenario": s.__name__, "pass": False, "error": str(e)}
        results.append(r)
        print(f"\n*** {s.__name__}: {'PASS' if r.get('pass') else 'FAIL'} → {r}")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for r in results:
        status = "PASS" if r.get("pass") else "FAIL"
        print(f"  {status}  scenario {r.get('scenario')}: {r}")

    n_pass = sum(1 for r in results if r.get("pass"))
    n_total = len(results)
    print(f"\n  {n_pass}/{n_total} passed")
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
