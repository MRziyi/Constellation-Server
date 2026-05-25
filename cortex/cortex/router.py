"""Cortex Router — turns an event into a dispatch plan.

Two entry points:
- `route_stub(event)`: hard-coded echo plan (Phase 1 fallback when no API key)
- `route(event, ...)`: real GPT call per CORTEX-ROUTER-PROMPT.md, via `llm_cache.cached_chat_create`

Prompt shape (v0.3, per Zack's "don't dump raw JSON to GPT, organize for readability"):
- System prompt is the directives + schema + multi-step / feedback / HUD body design (terse)
- User prompt is a natural-language brief composed by `_build_user_prompt`:
    THE ASK        — what the user said + when
    [WHAT HAPPENED ALREADY]  (only on multi-step continuation; compact)
    [ZACK'S WORDS ON THE PRIOR CARD]  (only when user spoke freely)
    ZACK'S DIGITAL TWIN — identity + skills + people/core inline
    YOUR TOOLS     — adapter list
    YOUR JOB       — output instructions (think then JSON in fence)

Token discipline: no event IDs / timestamps / opaque hashes that don't help the model.

Multi-call paradigm: this entry currently does ONE call per Router invocation. The
multi-step task paradigm (R-3) is at task level — each round of a multi-step task is
a fresh `route()` call with `task_history` accumulated. If a single round ever needs
multiple internal LLM calls (e.g., "first decide what to load, then plan"), wire that
here via `cached_chat_create` again; the cache layer dedups identical sub-calls.
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
  query   — read-only (list/read/search), no side effect
  draft   — TEXTUAL artefact, no system touch. ONLY for claude_code.draft and
            applescript_mail.draft (the two tools with draft semantics)
  execute — real side effect (add reminder, send email, …). Side-effecting actions
            are ALWAYS execute, never draft

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


# ── Available tools catalog (mirrors TOOL-ADAPTERS.md §1-13) ───────────────

AVAILABLE_TOOLS: dict[str, dict[str, str]] = {
    "echo": {
        "actions": "echo",
        "description": "Phase 1 debug: returns args.text verbatim.",
    },
    "applescript_reminders": {
        "actions": "add, list, complete, delete",
        "description": "Reminders.app. add(title, due?, list?, notes?).",
    },
    "applescript_calendar": {
        "actions": "list_today, list_range, add_event, find_conflict, get_event",
        "description": (
            "Calendar.app. add_event(title, start, end, calendar?='个人', location?, notes?) "
            "with ISO 8601 start/end."
        ),
    },
    "applescript_mail": {
        "actions": "read_current, list_inbox, find_messages, draft, send, get_thread",
        "description": (
            "Mail.app. Three send modes — pick correctly: "
            "REPLY → send(reply_to_current=true | reply_to_message_id, body); do NOT pass "
            "account (Mail auto-uses receiving account). "
            "COMPOSE → send(to, subject, body, account?='iCloud'|'Google'|'QQ'|'UIUC' — only "
            "if Zack named one). "
            "find_messages(participant?, subject_contains?, account?, mailbox?, limit?) "
            "returns message_ids for reply_to_message_id. "
            "send is preview-always; dry_run=true routes to Drafts."
        ),
    },
    "fs": {
        "actions": "read, write, append, grep, list, delete",
        "description": (
            "Local filesystem. read(path), write(path, content, mode?='overwrite'|'create_only'), "
            "append(path, content), delete(path), list(path), grep(pattern, path, include_vendored?). "
            "Args use 'content' for the bytes to write (NOT 'text'/'body'/'data'). "
            "Reads anywhere; writes under ~/constellation/, ~/Code/Projects/, /tmp/; deletes only "
            "under ~/constellation/twin/. grep auto-excludes .venv, node_modules, .git, etc."
        ),
    },
    "apple_notes": {
        "actions": "create, list, read, append, search",
        "description": (
            "Notes.app. create(title, body?, folder?) for 'drop a thought' captures; "
            "search(query) by title. Default account=iCloud, folder=Notes. Prefer over "
            "Reminders for prose / multi-line content."
        ),
    },
    "system_status": {
        "actions": "get",
        "description": (
            "Mac state: battery_pct, on_ac, focus_mode, frontmost_app, wifi_ssid, "
            "tailscale, now_iso, tz. Query before planning if intent depends on context."
        ),
    },
    "apple_shortcuts": {
        "actions": "list, run",
        "description": (
            "Run user-defined Apple Shortcuts. list() enumerates; run(name, input?) → stdout. "
            "Preview-always."
        ),
    },
    "twin_query": {
        "actions": "ask",
        "description": (
            "Semantic Q&A over Zack's Twin (grep + GPT synthesis with citations). "
            "ask(question) → {answer, snippets}. Use for '我之前对 X 怎么想的' style recall. "
            "Read-only; auto."
        ),
    },
    "imessage": {
        "actions": "send, list_recent",
        "description": (
            "iMessage. send(to:phone_or_email, body) preview-always. list_recent reads chat.db "
            "(needs FDA TCC; soft-errors if not granted)."
        ),
    },
    "safari_state": {
        "actions": "current_tab, all_tabs, recent_history",
        "description": (
            "Safari awareness. current_tab/all_tabs via AppleScript. recent_history reads "
            "History.db (needs FDA TCC)."
        ),
    },
    "claude_code": {
        "actions": (
            "draft, run, continue_, list_sessions, run_interactive, get_pane, "
            "send_keys, kill, list_tmux, start_watcher, stop_watcher, __test_inject_wake__"
        ),
        "description": (
            "Claude Code CLI. Two tracks — pick correctly: "
            "TRACK A `claude -p` (non-interactive): draft(prompt, working_dir?, add_dirs?) → "
            "one-shot text; run() tracks session_id for resume; continue_(session_id, prompt) "
            "resumes. Route web/paper search, code gen, summaries, 'read dir X and tell me' here. "
            "TRACK B tmux (interactive): run_interactive() spawns CC TUI with reverse-wake "
            "watcher; get_pane / send_keys(literal?) / kill drive it. Use for 'keep running, "
            "I'll come check' tasks and UC2 supervision. "
            "__test_inject_wake__ is test-only."
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
