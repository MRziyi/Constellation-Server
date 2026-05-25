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
#
# v2.6: added optional `phase_done`/`next` for multi-phase checkpointing.
# When `phase_done=true` AND `next` is non-empty, Cortex treats the output
# as a CHECKPOINT (blocking preview for user approval before next phase).
# Otherwise the output is FINAL and `actions[]` is what Cortex will preview.
CANONICAL_ACTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["actions"],
    "properties": {
        "actions": {
            "type": "array",
            "description": (
                "0+ side-effecting actions for Cortex to execute AFTER Zack "
                "confirms a preview. Empty for checkpoint outputs."
            ),
            "items": {"type": "object", "required": ["type"]},
        },
        "summary":    {"type": "string", "description": "one HUD line"},
        "notes":      {"type": "string", "description": "info NOT requiring action"},
        # Phase fields (only set at checkpoints):
        "phase_done": {"type": "boolean", "description": "true → just finished a phase; pause for Zack"},
        "next":       {"type": "string",  "description": "one-line plan for the next phase"},
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
    """Assemble the CC agent brief — v2.5, informed by code.claude.com best practices.

    Design discipline (per Anthropic Claude Code docs):
      - CLAUDE.md test: "Would removing this cause Claude to make mistakes?"
        If no — cut it. Long files get rules lost.
      - "YOU MUST" / "IMPORTANT" emphasis on hard rules (improves adherence).
      - Don't teach Claude its own tools (it knows shell, osascript, etc).
      - Provide verification criteria (schema = self-check).
      - Avoid infinite exploration; bound the research scope.

    What we DROPPED from v2:
      - Long role preamble ("You're the agent backend for Zack's hands-free AI")
      - ENVIRONMENT section (CC knows what its tools are)
      - HUD STYLE section (subsumed into R3 and won't change CC behaviour
        beyond what the rules already do).

    What we STRENGTHENED:
      - "YOU MUST" caps on the two non-negotiable rules
      - Explicit token-limit handling in R2 (this was the Kao stall)
      - Self-check checklist right after schema
      - R3 softened — best-effort mid-stream listening, since send_keys
        often doesn't land during extended thinking (per Zack 2026-05-25)
    """
    twin_slices = twin_slices or {}
    available_dirs = available_dirs or []

    L: list[str] = []

    # 1. TASK — first thing CC sees
    L.append("== TASK ==")
    L.append(f'Zack: "{ask_text}"')
    if now_iso:
        L.append(f"NOW: {now_iso}")
    if has_photo:
        L.append("PHOTO: attached (glass camera)")
    L.append("")

    # 2. Output contract — front-loaded with YOU MUST + verification step
    if output_schema is not None:
        if isinstance(output_schema, dict):
            schema_text = json.dumps(output_schema, ensure_ascii=False, indent=2)
        else:
            schema_text = str(output_schema)
        L.append("== YOU MUST OUTPUT EXACTLY THIS JSON AS YOUR FINAL MESSAGE ==")
        L.append("(no prose around it · no markdown fence · just the raw object)")
        L.append("")
        L.append(schema_text)
        L.append("")
        L.append("Action `type` values: email | reminder | calendar_event | imessage | fs_write | shortcut")
        L.append("Per-type fields: see APPENDIX (bottom).")
        L.append("")
        L.append("BEFORE you emit, self-check:")
        L.append("  ✓ JSON parses cleanly (paste into a parser mentally)")
        L.append("  ✓ each action has its required fields")
        L.append("  ✓ all times are ISO 8601 WITH timezone offset (e.g. 2026-05-27T14:00:00-05:00)")
        L.append("")
    else:
        L.append("== OUTPUT ==")
        L.append("Free-form text. Terse — Zack reads on a HUD.")
        L.append("")

    # 3. Rules — YOU MUST emphasis on the criticals; ≤3 to keep them salient
    L.append("== RULES ==")
    L.append("R1  YOU MUST NOT execute side effects yourself. SEND mail, ADD reminder,")
    L.append("    ADD calendar, SEND imessage, fs.write outside /tmp — all of these are")
    L.append("    forbidden. Propose them in actions[] only; Cortex executes after Zack")
    L.append("    confirms a preview. Read ops on any of those apps are fine.")
    L.append("")
    L.append("R2  YOU MUST emit actions[] even with partial info. If a Read fails with")
    L.append("    \"file too large\" or any token limit, STOP retrying that file — move on")
    L.append("    with what you already have. If truly nothing to propose, emit")
    L.append("    actions:[] + explain in notes:. Never go silent.")
    L.append("")
    L.append("R3  If a new \"user\" message appears in your conversation mid-task, it's")
    L.append("    Zack speaking live. Integrate before emitting JSON, best-effort. If you")
    L.append("    miss it (deep in thinking), that's OK — he'll correct at the preview.")
    L.append("")

    # 4. Bounded approach — soft budget; Claude Code docs warn about "infinite exploration"
    L.append("== APPROACH ==")
    L.append("Plan briefly in your thinking block, then act. Aim for ≤8 tool calls before")
    L.append("committing. If you find yourself at 6+ calls without a clear path, COMMIT")
    L.append("with what you have and use notes: to explain what's incomplete.")
    L.append("")

    # 5. Phase checkpoints — the multi-step blocking control loop (v2.6)
    L.append("== PHASES (when to checkpoint vs go straight to final) ==")
    L.append("If your work has 2+ distinct phases (e.g. \"check emails THEN find dir THEN")
    L.append("draft reply\"), CHECKPOINT between phases instead of running straight through.")
    L.append("This lets Zack confirm or redirect before each phase — critical for sensitive")
    L.append("operations or when a wrong keyword could send research the wrong direction.")
    L.append("")
    L.append("AT A CHECKPOINT, your final JSON for THIS turn must be:")
    L.append("  {")
    L.append('    "phase_done": true,')
    L.append('    "summary":    "<what you just finished, 1 line for HUD>",')
    L.append('    "found":      "<key findings, ≤3 sentences>",         // optional')
    L.append('    "next":       "<concrete one-line plan for next phase>",')
    L.append('    "actions":    []')
    L.append("  }")
    L.append("Then END YOUR TURN and wait. Zack will reply with one of:")
    L.append("  - \"continue\" / \"go\" / \"yes\" → proceed with next as stated")
    L.append("  - free-form text → redirect; integrate before next phase")
    L.append("  - \"cancel\" / \"stop\" → abandon; emit actions:[] + notes on next turn")
    L.append("")
    L.append("CHECKPOINT BEFORE: searching a directory you guessed at · acting on parsed")
    L.append("data that might be ambiguous · any keyword search where wrong terms waste")
    L.append("time · before drafting a sensitive reply.")
    L.append("")
    L.append("SKIP CHECKPOINT WHEN: the entire task is one bounded mechanical step")
    L.append("(\"remind me to X\", \"write Y to file Z\"). Go straight to final actions[].")
    L.append("")
    L.append("FINAL OUTPUT (only when truly done; no more phases) must have phase_done")
    L.append("absent or false, AND actions[] populated (or empty with notes explaining why).")
    L.append("That's how Cortex knows to stop calling you.")
    L.append("")

    # 5. Twin slices the selector picked (only what's relevant)
    if twin_slices:
        L.append("== ZACK'S TWIN (selector-picked, already loaded) ==")
        for path, content in twin_slices.items():
            L.append(f"=== {path} ===")
            L.append(content.rstrip())
            L.append("")
    else:
        L.append("== ZACK'S TWIN ==")
        L.append("(none pre-loaded; read ~/constellation/twin/ if needed)")
        L.append("")

    # 6. Available scopes — just paths, no lecture on tools
    if available_dirs:
        L.append("== AVAILABLE PATHS (--add-dir granted) ==")
        for d in available_dirs:
            L.append(f"  {d}")
        L.append("")

    # 6b. Apple ecosystem access (READ ONLY here — writes go through actions[])
    # Zack uses Apple Mail / Calendar / Reminders / Notes / Messages — NOT
    # Gmail / Google Calendar / Drive (those MCPs are deliberately disabled).
    # Query them via Bash + osascript. Examples:
    L.append("== APPLE ECOSYSTEM (read-only via Bash + osascript) ==")
    L.append("Zack lives in Apple's stack, not Google's. To search/read his stuff use")
    L.append("Bash with osascript. Common one-liners (treat as starting templates):")
    L.append("  # Recent Mail messages from / to / about someone")
    L.append("  osascript -e 'tell application \"Mail\"")
    L.append("    set cutoff to (current date) - (60 * days)")
    L.append("    repeat with acc in every account")
    L.append("      repeat with mb in every mailbox of acc")
    L.append("        try")
    L.append("          set msgs to (messages of mb whose date received > cutoff)")
    L.append("          repeat with m in msgs")
    L.append("            -- filter by sender, subject, recipient as needed")
    L.append("          end repeat")
    L.append("        end try")
    L.append("      end repeat")
    L.append("    end repeat")
    L.append("  end tell'")
    L.append("  # Today's calendar events")
    L.append("  osascript -e 'tell application \"Calendar\" to return summary of every event of every")
    L.append("    calendar whose start date ≥ (current date) and start date < ((current date) + days)'")
    L.append("  # Reminders lists")
    L.append("  osascript -e 'tell application \"Reminders\" to return name of every list'")
    L.append("  # Active Safari tab")
    L.append("  osascript -e 'tell application \"Safari\" to return URL of current tab of front window'")
    L.append("")
    L.append("Notes:")
    L.append("- Mail.app may take a few seconds on the first query (TCC + cold AppleScript).")
    L.append("- Mail's whose-clause filtering is faster than iterating then matching in shell.")
    L.append("- These are READ ops only. To send mail / add reminder / add event / send iMessage,")
    L.append("  EMIT an action in actions[] — Cortex executes after Zack confirms (R1).")
    L.append("- Do NOT try Gmail / Google Calendar / Google Drive MCP — disabled by design.")
    L.append("")

    # 7. Appendix — action shapes
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
