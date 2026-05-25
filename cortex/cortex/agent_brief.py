"""Single source of truth for the brief Cortex hands to Claude Code's TUI agent.

Per Zack 2026-05-25:
  - High information density. Every line earns its tokens.
  - No "in case you need it" pre-loading — CC gets lost.
  - Selector picks the Twin slices; only those inline.
  - Output schema appears ONCE, near the top, as actual JSON CC can copy.
  - Three rules, terse: propose-don't-execute · never bail · listen mid-task.

The brief produced here is passed verbatim to `claude_code.agent`; the
adapter no longer mutates it. If you want CC to behave differently, change
THIS file — not the adapter, and not the dev endpoint.
"""

from __future__ import annotations

import json
from typing import Any


# Default schema Cortex enforces when caller doesn't supply one. Tracks
# AGENT-ARCHITECTURE-V2 §4 and the executor mapping in
# cortex.server._action_to_subtask.
CANONICAL_ACTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["actions"],
    "properties": {
        "actions": {
            "type": "array",
            "description": (
                "0+ side-effecting actions for Cortex to execute AFTER Zack "
                "confirms a preview. Each item has a `type` discriminator."
            ),
            "items": {"type": "object", "required": ["type"]},
        },
        "summary": {"type": "string", "description": "one HUD line"},
        "notes":   {"type": "string", "description": "info NOT requiring action"},
    },
}


# Inlined verbatim — terse per-type field shapes. Critical fields only.
_ACTION_APPENDIX = """\
email          {type:"email",          to OR reply_to_message_id, subject (skip on reply), body, account?='iCloud'|'QQ'|'Google'|'UIUC'}
reminder       {type:"reminder",       title, due_iso?, list?, notes?}
calendar_event {type:"calendar_event", title, start_iso, end_iso, location?, notes?, calendar?}
imessage       {type:"imessage",       to=phone_or_email, body}
fs_write       {type:"fs_write",       path, content}     # only under ~/constellation/, ~/Code/Projects/, /tmp/
shortcut       {type:"shortcut",       name, input?}"""


def build_agent_brief(
    *,
    ask_text: str,
    now_iso: str | None = None,
    has_photo: bool = False,
    twin_slices: dict[str, str] | None = None,
    output_schema: dict[str, Any] | str | None = None,
    available_dirs: list[str] | None = None,
) -> str:
    """Assemble the CC agent brief. Tight; ~600-900 tokens typical.

    Args:
      ask_text:        what Zack just said, verbatim
      now_iso:         ISO timestamp (caller injects; keeps prompt deterministic)
      has_photo:       Glass camera attached an image
      twin_slices:     {path: content} from v0.5 selector — empty/None = none
      output_schema:   dict (rendered as JSON) OR str (rendered as-is) OR None
                       (no JSON contract — CC's final text is free-form)
      available_dirs:  paths CC has --add-dir on (just for awareness)
    """
    twin_slices = twin_slices or {}
    available_dirs = available_dirs or []

    L: list[str] = []

    # 1. Role + the ask (top of attention)
    L.append(
        "You're the agent backend for Zack's hands-free AI on AR glasses. He just"
        " spoke; you research, then emit ONE JSON object as your final message."
        " Cortex's HITL gate runs on that JSON — you do NOT execute side effects."
    )
    L.append("")
    L.append("== ASK ==")
    L.append(f'Zack: "{ask_text}"')
    if now_iso:
        L.append(f"NOW: {now_iso}")
    if has_photo:
        L.append("PHOTO: attached (glass camera)")
    L.append("")

    # 2. Output contract (BEFORE rules; CC sees the shape it must produce)
    if output_schema is not None:
        if isinstance(output_schema, dict):
            schema_text = json.dumps(output_schema, ensure_ascii=False, indent=2)
        else:
            schema_text = str(output_schema)
        L.append("== OUTPUT (your final message MUST be ONLY this JSON, no fence, no prose) ==")
        L.append(schema_text)
        L.append("")
        L.append("Action `type` values: email | reminder | calendar_event | imessage | fs_write | shortcut")
        L.append("Per-type fields in APPENDIX (bottom).")
        L.append("")
    else:
        L.append("== OUTPUT ==")
        L.append("Free-form text. Be terse — Zack reads on a HUD.")
        L.append("")

    # 3. Three rules (canonical for v2; tight one-liners)
    L.append("== RULES ==")
    L.append("R1  Propose, don't execute. Cortex fires actions[] only after Zack confirms.")
    L.append("R2  Never bail. Token limit / permission denied / ambiguity → emit actions[] from")
    L.append("    what you DO know; if truly stuck, return actions:[] + put the blocker in `notes`.")
    L.append("R3  Listen mid-task. New `user` messages mid-conversation = Zack correcting live.")
    L.append("    Integrate BEFORE final JSON; don't ask him to repeat.")
    L.append("")

    # 4. Twin slices the selector picked (only what's relevant)
    if twin_slices:
        L.append("== ZACK'S DIGITAL TWIN (selector-picked) ==")
        for path, content in twin_slices.items():
            L.append(f"=== {path} ===")
            L.append(content.rstrip())
            L.append("")
    else:
        L.append("== ZACK'S DIGITAL TWIN ==")
        L.append("(none pre-loaded; if you need style or contacts, read ~/constellation/twin/)")
        L.append("")

    # 5. Environment (terse — CC knows what shells + osascript are)
    L.append("== ENVIRONMENT ==")
    L.append("Read-anywhere via shell + osascript (Mail / Reminders / Calendar / Messages / Notes / Shortcuts).")
    if available_dirs:
        L.append("Granted --add-dir paths:")
        for d in available_dirs:
            L.append(f"  - {d}")
    L.append("")

    # 6. Style note for HUD live ticker
    L.append("== HUD STYLE ==")
    L.append("Your Bash `description` + assistant text stream live to Zack's HUD as 1-line tickers.")
    L.append("Short `description` (3-8 words, verb-led) = good. Long prose between tool calls = bad.")
    L.append("")

    # 7. Appendix
    if output_schema is not None:
        L.append("== APPENDIX — action shapes ==")
        L.append(_ACTION_APPENDIX)

    return "\n".join(L)


def estimate_size(brief: str) -> dict[str, int]:
    return {
        "chars": len(brief),
        "lines": brief.count("\n") + 1,
        "approx_tokens": len(brief) // 4,
    }
