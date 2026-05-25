"""Cortex Router — turns an event into a dispatch plan.

Two-pass architecture (v0.5, per PROMPT-DESIGN-V2.md):

  pass 1 ── select_twin_paths(event, twin, history?)
            small selector call (~1.5K tokens):  ASK + TOC table
            → {"paths": ["identity.md", "skills/X.md", ...]}

  pass 2 ── route(event, context_pack, ...)
            planner call (~4–7K tokens):  ASK + ONLY selected twin slices
                                            + AVAILABLE TOOLS
            → dispatch plan JSON

Why two-pass: eager-loading the whole Twin (v0.4) was wasting ~5K tokens of
unrelated policy text on every call, and the Twin only grows. The selector
picks 1–3 paths a median ask actually needs.

Three entry points:
- `select_twin_paths(...)`: pass-1 selector (NEW in v0.5)
- `route(...)`: pass-2 planner (callers pass the path-filtered context_pack)
- `route_stub(...)`: hard-coded echo plan (still used when --use-stub-router)

Telemetry: each pass goes through `cached_chat_create` with distinct `purpose`
tags ("selector" / "router") so the Console's prompt-inspector lists both.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import structlog

from .llm_cache import cached_chat_create, parse_json_response
from .schema import Event

log = structlog.get_logger(__name__)


DEFAULT_MODEL = "gpt-5.2"


SYSTEM_PROMPT = """\
You are Cortex Router, the brain of Zack's personal AI system "Constellation". Each
turn you read a brief and emit a dispatch plan as JSON. Cortex executes it with
local Mac tools and renders results on Zack's AR glasses.

RULES
1. Never side-effect without preview. Set requires_confirm=true on mutating
   subtasks, or leave it null for Cortex's confirm-policies to decide.
2. Use only tools in YOUR TOOLS. If a capability is missing, set
   primary_intent="unsupported" and name what's missing in `reasoning`.
3. When Zack names a person, look up people/core/<slug>.md in the Twin and pull
   `email:` / `phone:` from frontmatter. Never invent a contact.
4. Fewest subtasks that do the job.
5. ISO 8601 for date/time args; resolve "tomorrow" / "3pm" against NOW.

result_format
  query   — bounded state read (battery, current tab, today's events), no side effect
  execute — side effect (add reminder, send email, run shortcut, …)
  (`draft` is legacy — composition/research now lives in the agent path, not here)

OUTPUT — think briefly in plain text if useful, then emit JSON inside a ```json
fence (Cortex parses only the fence):

{
  "primary_intent": "kebab-case",
  "subtasks": [
    {
      "tool":   "<from YOUR TOOLS>",
      "action": "<documented action>",
      "args":   { ... ISO dates ... },
      "context_pack": [],
      "result_format": "draft" | "execute" | "query",
      "requires_confirm": true | false | null
    }
  ],
  "hud_response": {
    "kind":  "preview_action" | "hud_show",
    "icon":  "one of ✉ ⌖ ⚙ ✦ ✓",
    "title": "short, specific",
    "body_template": "markdown; supports {{subtasks[i].result.field}} interpolation",
    "options": ["1–4 button labels"]
  },
  // NOTE: never emit kind="tool_card". That kind is reserved for the reverse-wake
  // path which Cortex builds directly without invoking you. For user-initiated
  // side-effecting actions use preview_action; for pure info use hud_show.
  "reasoning": "one sentence",
  "task_continues": true | false,
  "next_step_hint": "(optional) free-text hint to FUTURE-YOU"
}

MULTI-STEP — use when an intent needs mid-task user judgment (e.g. "find the
meeting time with 云 in recent mail, add a reminder, then reply"; can't add the
reminder until Zack confirms the extracted time).
- First round: task_continues=true, subtasks read-only ONLY (query/draft, never
  execute mid-task). body_template surfaces findings; options like
  ["Proceed","Edit","Cancel"]. next_step_hint tells FUTURE-YOU what's next.
- Cortex yields a card; Zack responds. You're re-invoked with WHAT HAPPENED
  ALREADY and (if he spoke) ZACK'S WORDS appended. Output the next round.
- Max 5 rounds; round 5 must set task_continues=false.
- Simple intents → single-shot, no multi-step.

FREE-FORM FEEDBACK — when ZACK'S WORDS appears (mic is always-on; voice runs
parallel to ring-tap). Classify:
  (a) CONFIRM    "yes" / "对" / "go ahead"           → follow prior next_step_hint
  (b) CORRECTION "actually 4pm not 10am"             → fix the value, recompute
  (c) SKIP       "skip the reminder" / "that's all"  → TERMINAL plan: hud_show +
                 task_continues=false + empty subtasks + brief ack.
                 Skip is AUTHORITATIVE — never proceed "just in case".
  (d) INJECT     "the meeting is Thursday 4pm"       → use his value as truth
If ambiguous → treat as (a).

HUD BODY — every card is INFO + a yield point, not a yes/no question. body_template
must carry enough for Zack to judge / correct / skip without tapping.
  Bad:  "Send the reply to Jane?"
  Good: "Reply ready:\\n\\nHey Jane —\\n\\nI'll be there at 3.\\n\\n— Zack"
  Bad:  "Add reminder?"
  Good: "Reminder: meeting with 云, 5/29 14:00 (from 'CHI draft sync')"
"""


def _summarise_subtask_for_history(sub: dict[str, Any], result: dict[str, Any]) -> str:
    """One-line compact representation of an executed subtask for history blocks."""
    tool = sub.get("tool", "?")
    action = sub.get("action", "?")
    rf = sub.get("result_format", "?")
    # Compact result: prefer key fields; fall back to truncated JSON
    if isinstance(result, dict):
        # Pick a sensible 1-line summary based on common fields
        if "text" in result and isinstance(result["text"], str):
            res_s = f"text({len(result['text'])} chars): {result['text'][:120]!r}"
        elif "items" in result and isinstance(result["items"], list):
            res_s = f"items×{len(result['items'])}"
            if result["items"]:
                first = result["items"][0]
                if isinstance(first, dict):
                    res_s += f" (e.g. {next(iter(first.items()))})"
        elif "answer" in result and isinstance(result["answer"], str):
            res_s = f"answer: {result['answer'][:160]!r}"
        elif "content" in result and isinstance(result["content"], str):
            res_s = f"content({len(result['content'])} chars): {result['content'][:120]!r}"
        elif "error" in result:
            res_s = f"ERROR: {str(result['error'])[:120]}"
        else:
            # Compact non-trivial dict
            kv_preview = json.dumps(result, ensure_ascii=False)
            if len(kv_preview) > 200:
                kv_preview = kv_preview[:200] + "…"
            res_s = kv_preview
    else:
        res_s = str(result)[:200]
    return f"{tool}.{action}({rf}) → {res_s}"


def _build_user_prompt(
    event: Event,
    available_tools_block: str,
    context_pack: dict[str, str] | None = None,
    feedback_iteration: dict[str, Any] | None = None,
    task_history: list[dict[str, Any]] | None = None,
) -> str:
    """Compose the natural-language brief sent to the Router.

    Design (per Zack 2026-05-24): no event IDs, no timestamps, no opaque JSON dumps.
    Every section is information the model needs to plan, presented as it would read.
    """
    blocks: list[str] = []
    payload = event.payload or {}
    now_local = datetime.now().astimezone()

    # ── THE ASK ──
    blocks.append("THE ASK")
    if event.kind == "user_invoke":
        text = payload.get("text") or ""
        photo_tag = " (photo attached)" if payload.get("image") else ""
        if text:
            blocks.append(f'Zack said: "{text}"{photo_tag}')
        else:
            blocks.append(f"Zack triggered without speaking{photo_tag}.")
    elif event.kind == "tool_reverse_wake":
        # Rare — Cortex usually builds tool_card directly. Kept for completeness.
        from_tool = payload.get("from_tool", "?")
        wake_kind = payload.get("wake_kind", "?")
        blocks.append(f"{from_tool} woke us ({wake_kind}):")
        blocks.append((payload.get("context") or "")[:600])
    else:
        blocks.append(f"{event.kind}: {json.dumps(payload, ensure_ascii=False)[:400]}")
    blocks.append(f"NOW: {now_local.strftime('%Y-%m-%d %H:%M %Z')}")
    blocks.append("")

    # ── WHAT HAPPENED ALREADY (multi-step continuation) ──
    if task_history:
        blocks.append(f"WHAT HAPPENED ALREADY (planning round {len(task_history) + 1}/5)")
        for i, step in enumerate(task_history, start=1):
            blocks.append(f'R{i} — "{step.get("step_intent") or "?"}"')
            for sub, res in zip(step.get("subtasks", []), step.get("subtask_results") or []):
                blocks.append(f"  · {_summarise_subtask_for_history(sub, res)}")
            if hint := step.get("next_step_hint"):
                blocks.append(f'  · next_hint: "{hint}"')
            decision = step.get("user_decision") or "?"
            if fb := step.get("user_feedback_text"):
                blocks.append(f'  · Zack: "{fb}" (tapped {decision})')
            else:
                blocks.append(f"  · Zack: {decision} (ring-tap)")
        blocks.append("")

    # ── ZACK'S WORDS ON THE PRIOR CARD (free-form feedback) ──
    if feedback_iteration:
        blocks.append("ZACK'S WORDS ON THE PRIOR CARD")
        blocks.append(f'"{feedback_iteration.get("feedback_text", "")}"')
        blocks.append("Classify (a)confirm (b)correction (c)skip (d)inject and shape the next plan.")
        blocks.append("")

    # ── ZACK'S DIGITAL TWIN ──
    if context_pack:
        blocks.append("ZACK'S DIGITAL TWIN")
        for path, content in context_pack.items():
            blocks.append(f"=== {path} ===")
            blocks.append(content.rstrip())
            blocks.append("")

    # ── YOUR TOOLS ──
    blocks.append("YOUR TOOLS")
    blocks.append(available_tools_block.rstrip())
    blocks.append("")

    # ── YOUR JOB ──
    blocks.append("YOUR JOB")
    blocks.append("Plan one round. Optional brief reasoning, then JSON in a ```json fence.")

    return "\n".join(blocks)


def _validate_plan(plan: dict[str, Any], allowed_tools: set[str]) -> None:
    """Raises ValueError on any schema violation. Soft-defaults missing-but-cheap fields."""
    # reasoning is documentation-only; default it instead of crashing the whole plan
    plan.setdefault("reasoning", "(none)")
    for key in ("primary_intent", "subtasks", "hud_response"):
        if key not in plan:
            raise ValueError(f"missing required key: {key}")
    if not isinstance(plan["subtasks"], list):
        raise ValueError("subtasks must be a list")
    for i, st in enumerate(plan["subtasks"]):
        for k in ("tool", "action", "result_format"):
            if k not in st:
                raise ValueError(f"subtask[{i}] missing {k}")
        if st["tool"] not in allowed_tools:
            raise ValueError(
                f"subtask[{i}] uses unknown tool '{st['tool']}'. "
                f"Allowed: {sorted(allowed_tools)}"
            )
        if st["result_format"] not in ("draft", "execute", "query"):
            raise ValueError(f"subtask[{i}].result_format invalid: {st['result_format']}")
        st.setdefault("args", {})
        st.setdefault("context_pack", [])
        st.setdefault("requires_confirm", None)
    hud = plan["hud_response"]
    if hud.get("kind") not in ("preview_action", "hud_show", "tool_card"):
        raise ValueError(f"hud_response.kind invalid: {hud.get('kind')}")
    hud.setdefault("icon", "✦")
    hud.setdefault("title", "Constellation")
    hud.setdefault("body_template", "")
    hud.setdefault("options", [])
    plan.setdefault("task_continues", False)
    plan.setdefault("next_step_hint", None)
    if not isinstance(plan["task_continues"], bool):
        raise ValueError(f"task_continues must be bool, got {type(plan['task_continues'])}")


# ────────────────────────────────────────────────────────────────────────
# Selector pass (pass 1) — pick which Twin slices the planner needs
# ────────────────────────────────────────────────────────────────────────

SELECTOR_SYSTEM_PROMPT = """\
You are Cortex's Twin selector. Zack just spoke; pick which Twin files the
planner needs to plan the next action — and ONLY those.

Output JSON, nothing else: {"paths": ["identity.md", "skills/X.md", ...]}

Rules:
- Include identity.md ONLY if the ask is about Zack himself, his preferences,
  his style, or naming/addressing him. Mechanical asks (status, time, "open
  X") don't need it.
- Include people/core/<slug>.md ONLY when Zack names that person (by name or
  one of their aliases listed in the TOC).
- Include skills/X.md ONLY when X is directly relevant to the immediate ask.
  Don't pre-emptively grab adjacent skills "just in case".
- Maximum 5 paths. Median good answer is 1–3. ALL paths must be from the TOC.
- When unsure between two skills, pick the more specific one.
- Empty list is valid. Don't pad. Empty is better than wrong.

JSON only. No prose. No markdown fences.
"""


MAX_SELECTOR_PATHS = 5
SELECTOR_FALLBACK_PATHS = ["identity.md"]


def _build_selector_prompt(
    event: Event,
    twin_toc_table: str,
    task_history: list[dict[str, Any]] | None = None,
) -> str:
    """Compose the user prompt for the selector pass. Tiny on purpose."""
    blocks: list[str] = []
    payload = event.payload or {}

    blocks.append("THE ASK")
    text = payload.get("text") or ""
    has_image = bool(payload.get("image"))
    if event.kind == "user_invoke":
        line = f'Zack said: "{text}"' if text else "Zack triggered without speaking."
        if has_image:
            line += " (photo attached)"
        blocks.append(line)
    else:
        blocks.append(f"{event.kind}: {json.dumps(payload, ensure_ascii=False)[:200]}")
    blocks.append("")

    if task_history:
        blocks.append(f"PRIOR ROUNDS (compact, round {len(task_history) + 1}/5)")
        for i, step in enumerate(task_history, start=1):
            blocks.append(f'R{i} — "{step.get("step_intent") or "?"}"')
            results = step.get("subtask_results") or []
            for sub, res in zip(step.get("subtasks", []), results):
                blocks.append(f"  · {_summarise_subtask_for_history(sub, res)}")
            if fb := step.get("user_feedback_text"):
                blocks.append(f'  · Zack said: "{fb}"')
        blocks.append("")

    blocks.append("TWIN TOC")
    blocks.append(twin_toc_table)
    blocks.append("")

    blocks.append("YOUR JOB")
    blocks.append('Output JSON only: {"paths": [...]}')

    return "\n".join(blocks)


async def select_twin_paths(
    event: Event,
    twin_toc_table: str,
    toc_paths: set[str],
    model: str = DEFAULT_MODEL,
    task_history: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Pass 1: ask the LLM which Twin paths to load. Returns a validated list.

    Defensive — failures (parse / API / unknown paths) fall back to
    SELECTOR_FALLBACK_PATHS rather than raising; the planner still does
    something sensible with just identity.md.
    """
    user_prompt = _build_selector_prompt(event, twin_toc_table, task_history=task_history)
    try:
        raw = await cached_chat_create(
            model=model,
            messages=[
                {"role": "system", "content": SELECTOR_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            purpose="selector",
        )
        obj = parse_json_response(raw)
        if not isinstance(obj, dict):
            raise ValueError(f"selector output not a dict: {type(obj)}")
        paths = obj.get("paths")
        if not isinstance(paths, list):
            raise ValueError(f"selector output 'paths' not a list: {type(paths)}")
        # Filter: keep only paths present in the TOC; cap at MAX_SELECTOR_PATHS
        validated = [p for p in paths if isinstance(p, str) and p in toc_paths][:MAX_SELECTOR_PATHS]
        log.info(
            "selector.picked",
            n_in=len(paths) if isinstance(paths, list) else 0,
            n_kept=len(validated),
            paths=validated,
        )
        return validated
    except Exception as e:
        log.warning("selector.failed_fallback", error=str(e), error_type=type(e).__name__)
        # Filter the fallback against the TOC just in case identity.md was renamed
        return [p for p in SELECTOR_FALLBACK_PATHS if p in toc_paths]


# ────────────────────────────────────────────────────────────────────────
# Planner pass (pass 2) — emit the dispatch plan
# ────────────────────────────────────────────────────────────────────────


async def route(
    event: Event,
    available_tools_block: str,
    allowed_tools: set[str],
    model: str = DEFAULT_MODEL,
    context_pack: dict[str, str] | None = None,
    feedback_iteration: dict[str, Any] | None = None,
    task_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Real Router. Calls OpenAI via cached_chat_create; validates + returns plan dict.

    On error returns a fallback `hud_show` plan rather than raising — Cortex always
    has something to show the user.
    """
    user_prompt = _build_user_prompt(
        event,
        available_tools_block,
        context_pack=context_pack,
        feedback_iteration=feedback_iteration,
        task_history=task_history,
    )

    try:
        raw = await cached_chat_create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            purpose="router",
            # No response_format=json_object: we want reasoning preamble + JSON
            # in a fence. parse_json_response handles the extraction.
        )
        plan = parse_json_response(raw)
        _validate_plan(plan, allowed_tools)
        log.info("router.plan", primary_intent=plan["primary_intent"],
                 n_subtasks=len(plan["subtasks"]),
                 task_continues=plan.get("task_continues", False))
        return plan
    except Exception as e:
        log.error("router.failed", error=str(e), error_type=type(e).__name__)
        return _fallback_plan(str(e))


def _fallback_plan(reason: str) -> dict[str, Any]:
    return {
        "primary_intent": "router_fallback",
        "subtasks": [],
        "hud_response": {
            "kind": "hud_show",
            "icon": "✦",
            "title": "Cortex",
            "body_template": "I didn't catch that — try again?",
            "options": [],
        },
        "reasoning": f"Router fallback: {reason}",
        "task_continues": False,
        "next_step_hint": None,
    }


def route_stub(event: Event) -> dict[str, Any]:
    """Phase 1 stub Router: echoes the event's text.

    Used when OPENAI_API_KEY is unset OR --use-stub-router flag is on.
    """
    payload = event.payload or {}
    text = payload.get("text", "<no text>")

    return {
        "primary_intent": "echo",
        "subtasks": [
            {
                "tool": "echo",
                "action": "echo",
                "args": {"text": text},
                "context_pack": [],
                "result_format": "draft",
                "requires_confirm": False,
            }
        ],
        "hud_response": {
            "kind": "preview_action",
            "icon": "✓",
            "title": "Echo",
            "body_template": f"You said: {text}",
            "options": ["SEND", "FEEDBACK"],
        },
        "reasoning": "Phase 1 stub — echoes the input.",
        "task_continues": False,
        "next_step_hint": None,
    }


# ── Available tools catalog ────────────────────────────────────────────────
# Phase 5g (V2 pivot): pruned to the simple-path executor + state-query set.
# Research / composition / multi-call asks are routed to the agent path by the
# classifier BEFORE the planner ever runs (see classifier.py + server._dispatch_
# complex_agent). The planner's catalog is intentionally narrow — bounded
# single-call actions only.
#
# Adapter code in tool-agent retains the full action set (used by the agent
# path, and kept for regression safety). This catalog is just what the v0.5
# planner is allowed to NAME.

AVAILABLE_TOOLS: dict[str, dict[str, str]] = {
    "echo": {
        "actions": "echo",
        "description": "Debug: returns args.text verbatim.",
    },
    "applescript_reminders": {
        "actions": "add",
        "description": "Reminders.app. add(title, due?, list?, notes?) — bounded reminder.",
    },
    "applescript_calendar": {
        "actions": "add_event, list_today",
        "description": (
            "Calendar.app. add_event(title, start, end, calendar?='个人', location?, notes?) "
            "with ISO 8601 start/end. list_today() → events for current local day."
        ),
    },
    "applescript_mail": {
        "actions": "send",
        "description": (
            "Mail.app SEND only (composition / reading is agent territory). "
            "COMPOSE → send(to, subject, body, account?='iCloud'|'Google'|'QQ'|'UIUC' — only "
            "if Zack named one). REPLY → send(reply_to_current=true, body); do NOT pass "
            "account (Mail auto-uses receiving account). Preview-always."
        ),
    },
    "fs": {
        "actions": "write",
        "description": (
            "Local filesystem WRITE only (reading / searching is agent territory). "
            "write(path, content, mode?='overwrite'|'create_only'). 'content' is the bytes "
            "(NOT 'text'/'body'/'data'). Writes under ~/constellation/, ~/Code/Projects/, /tmp/."
        ),
    },
    "system_status": {
        "actions": "get",
        "description": (
            "Mac state snapshot: battery_pct, on_ac, focus_mode, frontmost_app, wifi_ssid, "
            "tailscale, now_iso, tz. Single-call bounded query."
        ),
    },
    "safari_state": {
        "actions": "current_tab",
        "description": "Safari awareness. current_tab() → {url, title}. Single-call bounded query.",
    },
    "apple_shortcuts": {
        "actions": "run",
        "description": "Run a named Apple Shortcut. run(name, input?) → stdout. Preview-always.",
    },
    "imessage": {
        "actions": "send",
        "description": "iMessage. send(to:phone_or_email, body). Preview-always.",
    },
    "claude_code": {
        "actions": "agent",
        "description": (
            "Escalate to the research-agent path (Claude Code in tmux). Use ONLY for asks "
            "the classifier should have caught but didn't — reading/searching/composing/"
            "multi-step. agent(text, working_dir?, add_dirs?) spawns the briefed CC session "
            "and produces a phase-checkpoint card. (agent_continue / agent_kill are "
            "orchestration-internal actions Cortex dispatches on user decisions; the "
            "planner doesn't emit them.)"
        ),
    },
}


def available_tools_block(enabled: set[str] | None = None) -> tuple[str, set[str]]:
    """Return (prompt_block, allowed_tool_names). Filtered by `enabled` if given."""
    items = AVAILABLE_TOOLS.items() if enabled is None else [
        (n, info) for n, info in AVAILABLE_TOOLS.items() if n in enabled
    ]
    lines: list[str] = []
    names: set[str] = set()
    for name, info in items:
        lines.append(f"{name:<24} actions: {info['actions']}")
        lines.append(f"{'':<24}  {info['description']}")
        names.add(name)
    return "\n".join(lines), names
