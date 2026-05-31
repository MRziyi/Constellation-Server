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
        "summary":    {"type": "string", "description": (
            "Your reply to Zack on the HUD. For a QUESTION / lookup (he asked "
            "something or wants info — no side effect) put the FULL answer here "
            "(may be multi-line); actions stays []. For an execute task it's a "
            "1-line recap. NEVER empty — empty summary + empty actions = Zack "
            "sees nothing."
        )},
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
    photo_path: str | None = None,
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
    if photo_path:
        # The glasses photo is ATTACHED to this message as an image block — the
        # agent sees it directly (Claude is multimodal). Do NOT pre-digest it to
        # text and do NOT tell the model what to "see": just hand it Zack's ask
        # + the image and let it do exactly that. The on-disk copy is so a memo
        # can embed the picture.
        L.append("PHOTO: a glasses photo is ATTACHED to this message — look at it directly")
        L.append(f"  and do exactly what Zack asked, nothing extra. It's also saved at")
        L.append(f"  twin/memos/{photo_path}; if a memo is the right output, write it under")
        L.append(f"  twin/memos/<slug>.md and embed the image: ![]({photo_path})")
    elif has_photo:
        L.append("PHOTO: attached to this message (glass camera) — look at it directly.")
    L.append("")

    # 2. Output contract — hand-written, terse. (We DON'T dump JSON Schema —
    # that's 30 lines of bloat for a 6-field contract. Show the actual JSON
    # CC must emit, with comments. CC parses this faster than schema.)
    if output_schema is not None:
        L.append("== OUTPUT (your FINAL message must be exactly this JSON; nothing around it) ==")
        L.append("```")
        L.append("{")
        L.append('  "summary":    "your reply to Zack — a QUESTION/lookup → the FULL answer here (multi-line ok), actions:[]; NEVER empty",  // required')
        L.append('  "actions":    [ ... ],                           // required; 0+ items, shapes below')
        L.append('  "notes":      "optional info Zack should know",  // optional')
        L.append('  "phase_done": true,                              // only at a phase checkpoint')
        L.append('  "next":       "1-line plan for next phase"       // only with phase_done')
        L.append("}")
        L.append("```")
        L.append("ANSWER vs ACTION — if Zack ASKED something / wants info (no side effect):")
        L.append("  actions:[] AND write the FULL answer in summary — that IS the deliverable")
        L.append("  (multi-line ok). Empty summary + empty actions = Zack sees nothing. Never that.")
        L.append("Action shapes (omit fields not needed):")
        L.append("  " + _ACTION_APPENDIX.replace("\n", "\n  "))
        L.append("Self-check before emitting: parses cleanly · required fields per type · all times ISO 8601 with TZ.")
        L.append("")
    else:
        L.append("== OUTPUT ==")
        L.append("Free-form text. Terse — Zack reads on a HUD.")
        L.append("")

    # 3. Rules — ≤2 critical rules with YOU MUST emphasis. R3 dropped (mid-
    # stream send_keys is unreliable; phase checkpoints handle correction).
    L.append("== RULES ==")
    L.append("R1  YOU MUST NOT execute side effects (mail send, reminder add, calendar add,")
    L.append("    imessage, fs.write outside /tmp and outside ~/constellation/twin/). Propose")
    L.append("    those in actions[]; Cortex previews + executes after Zack approves.")
    L.append("    EXCEPTION — Zack's OWN scratchpad: when he asks you to save a memo / note,")
    L.append("    write it DIRECTLY under ~/constellation/twin/ (e.g. twin/memos/<slug>.md)")
    L.append("    with your Write tool — no preview needed. Reads are always fine.")
    L.append("R2  YOU MUST always emit valid JSON. On Read/Bash failure or token limit: stop")
    L.append("    that path, commit with what you have, explain in notes:. Never go silent.")
    L.append("")

    # 4. Phases — when to checkpoint vs final. Compact.
    L.append("== PHASES ==")
    L.append('A "phase" is a logical step (e.g. find emails · then draft reply). End a phase')
    L.append("by emitting {phase_done:true, summary, next, actions:[]} and STOPPING your turn.")
    L.append('Zack replies "continue" (proceed with next as stated), free text (redirect),')
    L.append('or "kill" (abandon).')
    L.append("")
    L.append("CHECKPOINT before: ambiguous keyword searches · sensitive drafts · acting on")
    L.append('  parsed data you\'re unsure about. SKIP checkpoint when one bounded step ("remind')
    L.append('  me to X", "write Y to file Z"). FINAL turn: phase_done absent/false +')
    L.append("  actions[] populated (or empty + notes explaining).")
    L.append("Aim ≤8 tool calls per phase. At 6+ with no clear path → COMMIT, don't loop.")
    L.append("")

    # 5. Twin pointer — v2 redesign (2026-05-26). Four user-facing slots only.
    L.append("== ZACK'S TWIN  (~/constellation/twin/  — granted via --add-dir) ==")
    L.append("Layout (only these four slots — read ~/constellation/twin/README.md for the contract):")
    L.append("  identity.md             Zack's archive (name, accounts, voice, taste). Read when relevant.")
    L.append("  people/core/<slug>.md   one file per person; minimal frontmatter (aliases / relation /")
    L.append("                          email); body is prose. Read when he names someone.")
    L.append("  receipts/<date>.md      Cortex's daily action log (you don't need to read these).")
    L.append("  .claude/skills/<n>/SKILL.md  auto-discovered by you (Anthropic Agent Skills format).")
    L.append("                          Current: email-style, reminder-style, code-style.")
    L.append("")
    L.append("WRITING twin content via fs_write actions — FOLLOW the README contract:")
    L.append("  new person → people/core/<kebab-slug>.md, frontmatter is JUST `aliases: / relation:")
    L.append("    / email:` (only if known), no `type / created / share / confidence` bloat.")
    L.append("  new skill → .claude/skills/<name>/SKILL.md, Anthropic format (`description:` only).")
    L.append("  Don't invent top-level dirs. Don't seed placeholders.")
    L.append("")
    L.append("Read on demand — don't preemptively scan the whole Twin.")
    L.append("")

    # 6. Available scopes
    if available_dirs:
        L.append("== --add-dir PATHS ==")
        for d in available_dirs:
            L.append(f"  {d}")
        L.append("")

    # 6b. Apple ecosystem — Mail/Calendar/Reminders/Safari live LOCAL not Google.
    # One concise reminder + minimal starter snippet. CC knows osascript.
    L.append("== APPLE ECOSYSTEM (Mail/Calendar/Reminders/Notes/iMessage/Safari) ==")
    L.append("Zack uses Apple apps, NOT Google. Read via Bash + osascript (Gmail/GCal/Drive")
    L.append("MCPs are deliberately disabled). For Mail prefer whose-clauses for speed:")
    L.append('  tell application "Mail"')
    L.append("    set cutoff to (current date) - (60 * days)")
    L.append("    repeat with acc in every account")
    L.append("      repeat with mb in every mailbox of acc")
    L.append("        try")
    L.append("          set msgs to (messages of mb whose date received > cutoff)")
    L.append("          repeat with m in msgs -- filter by sender/subject as needed")
    L.append("          end repeat")
    L.append("        end try")
    L.append("      end repeat")
    L.append("    end repeat")
    L.append("  end tell")
    L.append("")
    L.append("MAIL PERFORMANCE (important — slow Mail queries trapped previous runs):")
    L.append("- Your first Mail search should return message IDs (e.g. `id of m`).")
    L.append("  Then fetch bodies BY ID in a SECOND osascript — way faster than")
    L.append("  iterating + body-reading in one pass.")
    L.append("- Do NOT let Bash background a slow osascript. If the timeout looks")
    L.append("  tight, pass `timeout: 60000` (60s) to Bash explicitly. Async/background")
    L.append("  results land in a file you then have to chase, costing minutes.")
    L.append("- One Mail.app whose-clause query over a 60-day window is normally 5-20s.")
    L.append("  If yours takes longer, narrow the mailbox / date range.")
    L.append("")
    L.append("Calendar/Reminders/Safari follow the same `tell application` pattern; CC knows.")
    L.append("Side effects → emit in actions[] per R1; don't tell-application-send-it.")
    L.append("")

    return "\n".join(L)


def estimate_size(brief: str) -> dict[str, int]:
    return {
        "chars": len(brief),
        "lines": brief.count("\n") + 1,
        "approx_tokens": len(brief) // 4,
    }


def build_modify_brief(
    *,
    prior_summary: str | None,
    prior_actions: list[dict[str, Any]] | None,
    prior_notes: str | None,
    modify_text: str,
    now_iso: str | None = None,
) -> str:
    """Compose the follow-up brief Cortex hands to CC when the user clicks
    Modify on an agent FINAL preview card.

    Sent as the next user turn in a RESUMED CC session (--session-id =
    prior cc_session_id). CC's conversation history is fully loaded by CC
    itself; we don't need to re-inline anything except a compact summary
    of what CC already proposed + the user's correction.
    """
    L: list[str] = []
    L.append("== MODIFICATION ==")
    L.append(f'Zack reviewed your last output and asks:  "{modify_text.strip()}"')
    if now_iso:
        L.append(f"NOW: {now_iso}")
    L.append("")
    L.append("== YOUR PRIOR OUTPUT (for reference; you already have full conversation history) ==")
    if prior_summary:
        L.append(f"Summary: {prior_summary[:300]}")
    if prior_actions:
        L.append("Actions you proposed:")
        for a in prior_actions[:8]:
            try:
                compact = json.dumps(a, ensure_ascii=False)
            except (TypeError, ValueError):
                compact = str(a)
            L.append(f"  · {compact[:300]}")
    if prior_notes:
        L.append(f"Notes: {prior_notes[:300]}")
    L.append("")
    L.append("== TASK ==")
    L.append("Emit the REVISED final JSON (same schema as before): summary, actions[],")
    L.append("optional notes. Re-use your prior research — you already did the Bash/Read")
    L.append("calls in this session, don't redo them unless Zack's correction genuinely")
    L.append("needs fresh info. Don't go silent — emit valid JSON.")
    L.append("")
    L.append("Output the JSON object directly as your final message; no prose around it.")
    return "\n".join(L)
