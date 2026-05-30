"""WebSocket server — Glass-facing endpoint.

Phase 2 Slice A: real Router (when OPENAI_API_KEY present) or stub fallback.
Phase 3+: full Hybrid Connection Model (see INTERFACE-CONTRACTS.md §1.6) with push wake.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import structlog
import websockets
from websockets.asyncio.server import ServerConnection

from . import ids
from .agent_brief import (
    CANONICAL_ACTIONS_SCHEMA as _CANONICAL_ACTIONS_SCHEMA,
    build_agent_brief as _assemble_agent_brief,
)
from .control_plane import ControlPlane
from .schema import Command, Event, RPCDispatch, RPCResult
from .router import available_tools_block, route, route_stub, select_twin_paths
from .twin import Twin


def _parse_confirm_policies(twin_root: Any) -> dict[str, str]:
    """Parse twin/_system/confirm-policies.md → {tool:action → policy}.

    Looks for the YAML block in the file, then extracts lines like:
      applescript_mail:send         : preview-always
      fs:read                       : auto

    Returns empty dict if file missing — caller falls back to 'preview-default'.
    """
    import re
    rules: dict[str, str] = {}
    # New canonical location (Twin v2 redesign, 2026-05-26). Falls back to
    # the legacy path if anyone hasn't migrated yet.
    policy_file = twin_root / "_system" / "confirm-policies.md"
    if not policy_file.exists():
        policy_file = twin_root / "skills" / "confirm-policies.md"
    if not policy_file.exists():
        return rules
    text = policy_file.read_text(encoding="utf-8")
    # Match `tool:action : policy` lines (allowing whitespace + optional quotes)
    pattern = re.compile(r'^\s*"?([\w*]+):([\w_*]+)"?\s*:\s*([\w-]+)\s*$', re.MULTILINE)
    for m in pattern.finditer(text):
        tool, action, policy = m.group(1), m.group(2), m.group(3)
        rules[f"{tool}:{action}"] = policy
    return rules


def _apply_confirm_policies(plan: dict[str, Any], rules: dict[str, str]) -> dict[str, Any]:
    """Post-Router override per confirm-policies.md + multi-step yield discipline.

    Rules:
      - `preview-always` → force subtask.requires_confirm=true + (if any subtask is
        preview-always) bump HUD kind to preview_action.
      - `auto`           → force subtask.requires_confirm=false.
      - `preview-default`→ leave Router's choice unless Router said null, then true.
      - `deny`           → mark subtask with diagnostics; Cortex will skip dispatch.
      - unknown / wildcard fallback → preview-default.

    Returns the same plan, mutated in place (caller may keep reference).
    """
    forced_preview = False
    for st in plan.get("subtasks", []):
        key = f"{st.get('tool')}:{st.get('action')}"
        policy = rules.get(key) or rules.get(f"{st.get('tool')}:*") or rules.get("*") or "preview-default"
        if policy == "preview-always":
            st["requires_confirm"] = True
            st["_confirm_policy"] = "preview-always"
            forced_preview = True
        elif policy == "auto":
            st["requires_confirm"] = False
            st["_confirm_policy"] = "auto"
        elif policy == "deny":
            st["_confirm_policy_denied"] = True
        else:  # preview-default
            if st.get("requires_confirm") is None:
                st["requires_confirm"] = True
            st["_confirm_policy"] = policy

    if forced_preview and plan.get("hud_response", {}).get("kind") == "hud_show":
        plan["hud_response"]["kind"] = "preview_action"
        if not plan["hud_response"].get("options"):
            plan["hud_response"]["options"] = ["Proceed", "Edit", "Cancel"]
    return plan

log = structlog.get_logger(__name__)


# Default emoji per progress stage — adapter usually provides explicit `icon`
# but we cover bases when it doesn't (e.g. a different adapter starts pushing
# agent_progress in the future).
def _is_checkpoint(structured: Any) -> bool:
    """True if CC's structured output indicates a phase checkpoint (more to come).
    See AGENT-ARCHITECTURE-V2 §6 multi-phase pattern."""
    if not isinstance(structured, dict):
        return False
    return bool(structured.get("phase_done")) and bool((structured.get("next") or "").strip())


_STAGE_DEFAULT_ICONS: dict[str, str] = {
    "started":             "🤖",
    "brief_sent":          "▶️",
    "tool_call":           "🔧",
    "tool_result":         "✓",
    "assistant_text":      "💭",
    "thinking":            "💭",   # extended-thinking heartbeat (Opus is silent on disk)
    "completed":           "🎯",
    "error":               "✗",
    "feedback_noted":      "👂",
    "feedback_injected":   "💬",
}

def _default_icon_for_stage(stage: str) -> str:
    return _STAGE_DEFAULT_ICONS.get(stage, "·")


# ── Agent result → preview card / executor subtasks ───────────────────────
#
# CC's structured output uses the `actions[]` shape from
# AGENT-ARCHITECTURE-V2 §4. Each action maps to one of our 7 executor
# adapters; the preview card iterates them as rows; SEND fires them all
# in order via the existing _execute_remaining path.

_ACTION_ICONS: dict[str, str] = {
    "email":          "✉",
    "reminder":       "🔔",
    "calendar_event": "📅",
    "imessage":       "💬",
    "fs_write":       "📄",
    "shortcut":       "⚡",
}


def _action_to_subtask(action: dict[str, Any]) -> dict[str, Any] | None:
    """Map one action dict to the dispatch subtask shape _execute_remaining
    expects. Returns None on unknown action type (silently skipped)."""
    t = (action or {}).get("type")
    if t == "email":
        args: dict[str, Any] = {"body": action.get("body", "")}
        if action.get("reply_to_message_id"):
            args["reply_to_message_id"] = action["reply_to_message_id"]
        else:
            args["to"] = action.get("to", "")
            args["subject"] = action.get("subject", "")
            if action.get("account"):
                args["account"] = action["account"]
        return {
            "tool": "applescript_mail", "action": "send", "args": args,
            "context_pack": [], "result_format": "execute",
            "requires_confirm": False,   # the agent's preview WAS the confirm
        }
    if t == "reminder":
        args = {"title": action.get("title", "")}
        if action.get("due_iso"):
            args["due"] = action["due_iso"]
        if action.get("list"):
            args["list"] = action["list"]
        if action.get("notes"):
            args["notes"] = action["notes"]
        return {
            "tool": "applescript_reminders", "action": "add", "args": args,
            "context_pack": [], "result_format": "execute",
            "requires_confirm": False,
        }
    if t == "calendar_event":
        args = {
            "title": action.get("title", ""),
            "start": action.get("start_iso", ""),
            "end": action.get("end_iso", ""),
        }
        for k in ("location", "notes", "calendar"):
            if action.get(k):
                args[k] = action[k]
        return {
            "tool": "applescript_calendar", "action": "add_event", "args": args,
            "context_pack": [], "result_format": "execute",
            "requires_confirm": False,
        }
    if t == "imessage":
        return {
            "tool": "imessage", "action": "send",
            "args": {"to": action.get("to", ""), "body": action.get("body", "")},
            "context_pack": [], "result_format": "execute",
            "requires_confirm": False,
        }
    if t == "fs_write":
        return {
            "tool": "fs", "action": "write",
            "args": {"path": action.get("path", ""), "content": action.get("content", "")},
            "context_pack": [], "result_format": "execute",
            "requires_confirm": False,
        }
    if t == "shortcut":
        return {
            "tool": "apple_shortcuts", "action": "run",
            "args": {"name": action.get("name", ""), "input": action.get("input")},
            "context_pack": [], "result_format": "execute",
            "requires_confirm": False,
        }
    return None


def _render_actions_preview(actions: list[dict[str, Any]], summary: str | None = None, notes: str | None = None) -> str:
    """Render a glanceable markdown body for the preview card."""
    lines: list[str] = []
    if summary:
        lines.append(f"**{summary}**")
        lines.append("")
    for i, a in enumerate(actions, start=1):
        t = (a or {}).get("type", "?")
        icon = _ACTION_ICONS.get(t, "·")
        if t == "email":
            who = a.get("to") or "(reply)"
            subj = a.get("subject") or "(no subject)"
            body_snip = (a.get("body") or "")[:140].replace("\n", " ⏎ ")
            lines.append(f"**{i}. {icon} email → {who}**")
            lines.append(f"   *{subj}*")
            lines.append(f"   {body_snip}{'…' if len(a.get('body') or '') > 140 else ''}")
        elif t == "reminder":
            title = a.get("title", "?")
            due = a.get("due_iso", "no time")
            lines.append(f"**{i}. {icon} reminder** — {title} *(due {due})*")
        elif t == "calendar_event":
            title = a.get("title", "?")
            start = a.get("start_iso", "?")
            end = a.get("end_iso", "?")
            loc = a.get("location")
            line = f"**{i}. {icon} event** — {title} *({start} → {end})*"
            if loc:
                line += f" @ {loc}"
            lines.append(line)
        elif t == "imessage":
            who = a.get("to") or "?"
            body_snip = (a.get("body") or "")[:100].replace("\n", " ⏎ ")
            lines.append(f"**{i}. {icon} iMessage → {who}** — {body_snip}")
        elif t == "fs_write":
            path = a.get("path", "?")
            n = len(a.get("content") or "")
            lines.append(f"**{i}. {icon} write file** — `{path}` ({n}c)")
        elif t == "shortcut":
            name = a.get("name", "?")
            lines.append(f"**{i}. {icon} run shortcut** — {name}")
        else:
            lines.append(f"**{i}. {icon} {t}** — {json.dumps(a, ensure_ascii=False)[:120]}")
        lines.append("")
    if notes:
        lines.append(f"_{notes}_")
    return "\n".join(lines).strip() or "(no actions)"


class ResumeFailed(Exception):
    """Raised when claude_code.agent resume fails (jsonl missing, CC spawn
    error, etc.). Distinct from generic Exception so the caller can decide
    whether to fall back to v0.5 (yes for this) vs just log (no for WSS
    drops / delivery hiccups)."""


# ── Three-option decision canonicalization (Zack 2026-05-25 v2) ───────────
# Every blocking card has exactly three buttons: Approve / Modify / Kill.
# Free-text on the feedback channel (typed in the composer, or spoken via
# STT on Glass) gets classified into the same three outcomes server-side so
# the user doesn't have to click — "ok, go" is approve; "停" / "kill it" is
# kill; anything else substantive is modify-with-content.
#
# Approve = proceed exactly as previewed (send / continue / execute / yes).
# Modify  = redirect with details (the text is the redirection).
# Kill    = abandon: kill any agent tmux + drop pending + log a kill signal.
_THREE_OPTIONS = ["Approve", "Modify", "Kill"]

# Q.4.5 / C-47: tools that opt into receiving event.payload.image as
# `_image_b64` in their args. The router decides per-prompt whether to route
# to one of these (text-prompt-based, NEVER inspects image content). Other
# tools never see the image even when one is attached — that's the
# "default-off" guarantee Zack asked for (image is a passthrough, not a
# broadcast). Extend this set when new vision tools land (OCR, face-id, etc.).
_VISION_AWARE_TOOLS: set[str] = {"vision_describe"}

# R-13 / C-55: regex heuristic for "user's text looks like a visual question".
# Used by _handle_user_invoke to decide whether to pre-pull a scene capture
# from glass before classification. Intentionally generous on both English
# and Chinese — false positives waste a single capture (~70 KB / ~1.5s),
# false negatives just yield text-only answers. Both acceptable.
_VISUAL_INTENT_PATTERNS = [
    # English: what/which/can-you-see/describe/read/recognize + reference to
    # something in front of / on the desk / in the picture
    re.compile(
        r"\b(what(?:'s| is)?|describe|read|recognize|identify|see|look at|tell me about)\b"
        r".*\b(this|that|these|those|here|there|in front|on (?:the|my) (?:desk|table|screen|wall|monitor)|"
        r"the (?:screen|monitor|image|picture|photo|sign|text|object|thing|menu|page|document))\b",
        re.IGNORECASE,
    ),
    # English: explicit "what's in front" / "what do you see"
    re.compile(r"\b(what'?s in front|what do you see|what am i looking at)\b", re.IGNORECASE),
    # Chinese: 看 (see/look) / 这是什么 / 那是什么 / 前面 / 屏幕
    re.compile(r"(看一?下|这是什么|那是什么|前面|这个是|那个是|屏幕上)"),
]


def _looks_visual_intent(text: str) -> bool:
    """True iff the user's text reads like a visual question that would
    benefit from a scene capture. Used to gate the C-55 upfront image pull.
    Conservative regex; no LLM call."""
    if not text:
        return False
    for pat in _VISUAL_INTENT_PATTERNS:
        if pat.search(text):
            return True
    return False


# Cues that the user wants to READ TEXT in the frame (poster / sign / menu /
# document / "what does it say"). These justify OpenAI `detail:"high"` (image
# tiled at 512px so small text is legible). Everything else → `detail:"low"`
# (flat ~85 input tokens — enough for a scene glance, much cheaper). See
# vision_describe DEFAULT_DETAIL note.
_VISION_TEXT_READ_PATTERNS = [
    re.compile(
        r"\b(read|reads?|says?|written|text|word|wording|caption|title|label|"
        r"poster|sign|menu|document|page|paper|slide|receipt|card|book|article|"
        r"headline|paragraph|spell|transcribe|ocr)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(读|念|写的什么|写着什么|上面.*字|文字|海报|招牌|牌子|菜单|文件|文档|标题|说明|条款|这段|这页|纸上)"),
]


def _vision_detail_for(text: str) -> str:
    """Pick OpenAI vision `detail` by intent: `high` when the user wants to read
    text in the frame (poster/sign/menu/doc), else `low` to save tokens. No LLM
    call — a regex on the ask text."""
    if text:
        for pat in _VISION_TEXT_READ_PATTERNS:
            if pat.search(text):
                return "high"
    return "low"


# Per-conversation permission mode (Zack 2026-05-30). The agent's CC permission
# mode is decided from the CREATING utterance and sticks for the conversation:
#   - explicit "auto-run everything / I approve, just go" → bypassPermissions
#     (CC runs every tool with no prompt).
#   - default, or "I need to verify this" → acceptEdits ("edit mode"): CC
#     auto-accepts file edits but PROMPTS for Bash/exec/other — and those prompts
#     surface to Zack as checkpoint cards (Piece 2). Answer-needs (AskUserQuestion)
#     surface as question cards (Piece 3).
_AUTO_RUN_PATTERNS = [
    re.compile(
        r"(全自动|都自动|自动跑|全部(自动|批准|放行|跑)|都(批准|放行)|不用(问|确认)我?|"
        r"无需确认|不用经过我|放手去做|你看着办|随便你|"
        r"auto[-\s]?(run|approve|pilot|execute)|run\s+everything|"
        r"don'?t\s+ask|no\s+confirm|without\s+asking|just\s+do\s+it|yolo)",
        re.IGNORECASE,
    ),
]


def _permission_mode_for(text: str) -> str:
    """Decide the CC permission mode for a fresh conversation from its first
    utterance. Explicit auto-run → 'bypassPermissions'; otherwise 'acceptEdits'
    (the default 'edit mode' — prompts for non-edit tools, which become cards)."""
    if text:
        for p in _AUTO_RUN_PATTERNS:
            if p.search(text):
                return "bypassPermissions"
    return "acceptEdits"


def _card_type_for(options: list[str]) -> str:
    """Piece 4 — the 3 formal card types, derived from the options the glass
    will render (so the client can switch rendering + ring-mapping cleanly):
      - no options                → 'notification'  (dismiss only)
      - exactly ['answer']        → 'question'      (single answer → mic)
      - anything else             → 'checkpoint'    (approve / modify / reject|kill)
    """
    low = [str(o).strip().lower() for o in (options or [])]
    if not low:
        return "notification"
    if low == ["answer"]:
        return "question"
    return "checkpoint"


_TWIN_MEMO_ASSETS = os.path.expanduser("~/constellation/twin/memos/assets")


def _persist_image_to_twin(b64: str, tag: str) -> dict[str, str] | None:
    """UC1: decode a glasses photo (base64 JPEG) onto disk under
    twin/memos/assets/ so the agent can EMBED it in a memo (a captured poster /
    whiteboard / etc.). Cortex holds the bytes already, so it writes the file
    directly — far cheaper than pushing ~130 KB of base64 through the agent
    brief, and the agent only ever sees a short relative path. Returns
    {abs, rel_to_memos, bytes} or None on failure."""
    if not b64:
        return None
    import base64 as _b64
    try:
        raw = _b64.b64decode(b64)
    except Exception:
        return None
    if not raw:
        return None
    try:
        os.makedirs(_TWIN_MEMO_ASSETS, exist_ok=True)
        fname = f"img-{tag}.jpg"
        abs_path = os.path.join(_TWIN_MEMO_ASSETS, fname)
        with open(abs_path, "wb") as f:
            f.write(raw)
    except OSError:
        return None
    # Memos live in twin/memos/<name>.md, so a sibling assets/ ref is relative.
    return {"abs": abs_path, "rel_to_memos": f"assets/{fname}", "bytes": str(len(raw))}


# R-14.b / C-56: pin/unpin intent detection. Tiny regex on the user's
# voice-invoke text — when the entire utterance is a pin command, we
# short-circuit the normal classifier+dispatch flow, flip the session's
# `pinned` flag, emit a confirmation card, and stop. Pin patterns cover EN
# + ZH; the test is "is the ENTIRE prompt a pin instruction?" — not "does
# it contain pin words" (which would false-positive on "pin a reminder
# about X").
_PIN_INTENT_PATTERNS = [
    # English
    re.compile(r"^\s*(please\s+)?(pin|keep)( this| it| this one| this session)?\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*keep( this)? alive\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*don'?t (kill|drop|evict) this\.?\s*$", re.IGNORECASE),
    # Chinese
    re.compile(r"^\s*(请\s*)?(钉住|留着|保留|别(关|掐|杀)|不要(关|掐|杀))\s*(这个|此|它|这条|这个会话|这个对话|这个 session)?\s*[。.!！]?\s*$"),
]
_UNPIN_INTENT_PATTERNS = [
    re.compile(r"^\s*(please\s+)?unpin( this| it| this one| this session)?\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*release( this)?\.?\s*$", re.IGNORECASE),
    # Chinese
    re.compile(r"^\s*(取消(钉住|保留)|不(钉|留)了|解除(钉住|保留))\s*(这个|此|它)?\s*[。.!！]?\s*$"),
]


def _looks_pin_intent(text: str) -> bool:
    """True iff the user's text is the WHOLE pin command (not just contains
    pin words). False positives would spuriously pin sessions when the user
    asks to "pin a reminder about X"; the anchored regex prevents that."""
    if not text:
        return False
    return any(p.search(text) for p in _PIN_INTENT_PATTERNS)


def _looks_unpin_intent(text: str) -> bool:
    """True iff the user's text is the WHOLE unpin command."""
    if not text:
        return False
    return any(p.search(text) for p in _UNPIN_INTENT_PATTERNS)


# R-14 / C-56: derive a short stable session title from the first prompt or
# summary. Used by the session router LLM to let the user voice-reference
# past sessions ("the auth refactor one"). 8-word cap, strips trailing
# punctuation; lowercases nothing — preserves proper nouns.
def _derive_session_title(text: str) -> str:
    if not text:
        return "(untitled)"
    t = text.strip()
    # Drop common "instruction prefixes" that aren't part of the topic.
    for prefix in (
        "please ", "Please ", "could you ", "Could you ", "can you ", "Can you ",
        "i want to ", "I want to ", "help me ", "Help me ",
        "帮我", "请", "麻烦", "我想", "我要",
    ):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    # Cap at 8 English words OR 16 Chinese characters.
    words = t.split()
    if len(words) > 8:
        t = " ".join(words[:8]) + "…"
    if len(t) > 60:
        t = t[:60] + "…"
    # Strip trailing punctuation.
    t = t.rstrip("?。.!?,，；;：:")
    return t or "(untitled)"


_APPROVE_BUTTON_TOKENS = {
    # English
    "approve", "send", "send all", "continue", "ok", "yes", "go", "go ahead",
    "proceed", "confirm", "looks good", "lgtm",
    # 中文
    "确认", "确定", "继续", "没问题", "好的", "好", "对", "可以", "行", "通过",
}
_MODIFY_BUTTON_TOKENS = {
    "modify", "feedback", "adjust", "edit", "fix", "change",
    "修改", "改", "编辑",
}
_KILL_BUTTON_TOKENS = {
    "kill", "stop", "abort", "abandon", "cancel", "nevermind", "drop", "scrap",
    "掐断", "停", "停下", "取消", "算了", "别做了", "终止", "中断", "撤销",
}
# Phrases that, even when sent as free text, mean "approve as-is".
_LEARNING_QUEUE_REL = "_system/learning_queue.jsonl"


def _append_learning_signal(
    twin_root: Any,
    *,
    event: Event,
    pending: dict[str, Any],
    decision_kind: str,         # "approve" | "modify"
    correction_text: str | None,
) -> None:
    """Append one Approve/Modify decision to the implicit-learning queue.

    The queue is `~/constellation/twin/_system/learning_queue.jsonl`. A future
    Insight-Engine pass (Phase 7) reads this corpus and distills SKILL.md
    entries for ~/constellation/twin/.claude/skills/ when stable patterns
    emerge from Zack's corrections.

    Per Zack 2026-05-25: skills should not be hand-curated placeholders —
    they should be derived from real Approve/Modify interactions where Zack
    pushed back and steered the agent. This function captures the raw
    training signal; skill-generation happens elsewhere later.

    Schema (one JSON object per line):
      {
        "ts":              ISO-8601 UTC,
        "event_id":        evt_*,
        "user_ask":        original Zack ask (text),
        "agent_intent":    primary_intent the agent settled on,
        "agent_proposal":  compact summary of the actions / hud body shown,
        "decision":        "approve" | "modify",
        "correction":      Zack's modify text (None when approve),
        "was_checkpoint":  bool — phase-pause vs final preview,
        "was_agent_path":  bool — true if CC produced this; false if v0.5 planner.
      }
    Best-effort: silently skip on write failure (we never want a learning-log
    failure to break the decision flow).
    """
    try:
        # Resolve the path defensively — twin_root may be None or non-path-like.
        twin_path = getattr(twin_root, "root", None) or getattr(twin_root, "path", None) or twin_root
        if twin_path is None:
            return
        from pathlib import Path as _Path
        p = _Path(str(twin_path)) / _LEARNING_QUEUE_REL
        p.parent.mkdir(parents=True, exist_ok=True)

        plan = pending.get("plan") or {}
        hud = plan.get("hud_response") or {}
        subtasks = plan.get("subtasks") or []
        proposal_compact = {
            "title": hud.get("title"),
            "body": (hud.get("body_template") or "")[:400],
            "subtasks": [
                {"tool": s.get("tool"), "action": s.get("action"), "args": s.get("args")}
                for s in subtasks
            ],
        }

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_id": event.id,
            "user_ask": ((event.payload or {}).get("text") or "")[:600],
            "agent_intent": plan.get("primary_intent"),
            "agent_proposal": proposal_compact,
            "decision": decision_kind,
            "correction": (correction_text or "")[:1000] or None,
            "was_checkpoint": bool(pending.get("is_checkpoint")),
            "was_agent_path": bool(pending.get("from_agent") or pending.get("agent_result")),
        }
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("learning_queue.append_failed", error=str(e))


_APPROVE_FREE_TEXT_PREFIXES = (
    "ok", "yes", "go ahead", "go", "proceed", "looks good", "lgtm",
    "continue", "approve", "confirm",
    "好的", "好", "可以", "继续", "确认", "确定", "没问题", "通过", "行",
)
_KILL_FREE_TEXT_PHRASES = (
    "kill it", "kill this", "stop it", "stop this", "abort", "abandon this",
    "cancel this", "never mind", "nevermind", "drop it", "scrap it",
    "forget it", "don't do it", "do not do",
    "停下", "停止", "掐断", "算了", "别做了", "取消这个", "终止", "中断", "撤销",
)


def _classify_user_decision(
    decision: str | None,
    feedback_text: str | None,
) -> tuple[str, str | None]:
    """Return ('approve' | 'modify' | 'kill', resolved_text_for_modify_or_None).

    - Button click: `decision` is the button label. Map via the token sets.
      A button-click "Modify" with empty feedback_text → ('modify', None);
      caller must re-surface the card.
    - Free text: `decision` may be a generic marker like "feedback" or "send"
      while `feedback_text` carries the actual content. The free-text content
      itself is checked for approve-like / kill-like phrases.
    """
    d = (decision or "").strip().lower()
    if d in _APPROVE_BUTTON_TOKENS:
        return "approve", None
    if d in _KILL_BUTTON_TOKENS:
        return "kill", None
    if d in _MODIFY_BUTTON_TOKENS:
        ftext = (feedback_text or "").strip()
        return "modify", ftext or None

    # No clear button match. Treat as free-text channel — content drives.
    text = (feedback_text or decision or "").strip()
    if not text:
        # Empty signal in the free-text channel → modify-needs-text.
        return "modify", None
    tl = text.lower()
    if tl in _APPROVE_BUTTON_TOKENS:
        return "approve", None
    if tl in _KILL_BUTTON_TOKENS:
        return "kill", None
    # Kill-phrase match (substring within short utterance).
    if len(tl) <= 40 and any(phrase in tl for phrase in _KILL_FREE_TEXT_PHRASES):
        return "kill", None
    # Short utterances starting with an approve phrase (and nothing
    # substantive after) → approve.
    for prefix in _APPROVE_FREE_TEXT_PREFIXES:
        if tl == prefix or tl.startswith(prefix + " ") or tl.startswith(prefix + ",") or tl.startswith(prefix + "."):
            # If it's just the prefix + a short tail like "go ahead and send", treat as approve.
            tail = tl[len(prefix):].strip(" ,.!。，！")
            if not tail or tail in {"send", "send it", "send all", "and send", "and continue"}:
                return "approve", None
            # Substantive tail → fall through to modify with the full text.
            break
    return "modify", text


class CortexServer:
    """Single-process server holding the active Glass connection + Tool Agent connection."""

    def __init__(
        self,
        twin: Twin,
        tool_agent_url: str = "ws://localhost:8889",
        router_model: str = "gpt-5.2",
        use_stub_router: bool = True,
        plane: ControlPlane | None = None,
    ):
        self.twin = twin
        self.tool_agent_url = tool_agent_url
        self.router_model = router_model
        self.use_stub_router = use_stub_router
        self.plane = plane
        self._glass_conn: ServerConnection | None = None
        self._tool_conn: Any = None  # websockets client
        self._tool_reader_task: asyncio.Task | None = None
        # Pending RPC dispatches awaiting their RPCResult (rpc_id → Future).
        self._pending_rpcs: dict[str, asyncio.Future] = {}
        self._pending_previews: dict[str, dict[str, Any]] = {}
        # v2 agent runtime — tracks active claude_code.agent dispatches so
        # progress_feedback events from Glass can be routed to the right CC
        # tmux session via send-keys. Keyed by parent_event_id.
        self._active_agents: dict[str, dict[str, Any]] = {}
        # P0.1 — long-lived CC per HUD session. After an agent dispatch ends
        # (FINAL with actions, checkpoint pause, or research-only), the tmux
        # is kept alive (via tool-agent's keep_alive_on_final flag). The next
        # invoke in the same HUD session reuses it via agent_continue
        # (paste-buffer in 100-200ms) instead of spawning a fresh CC TUI
        # (5-8s cold start + brief re-load).
        #   key:    HUD session_id (ses_<hex>)
        #   value:  {tmux_session, cc_session_id, working_dir, timeout_s,
        #            last_activity (epoch s), last_summary?}
        self._active_hud_session_tmux: dict[str, dict[str, Any]] = {}
        # R-14.c: pending session-route confirmation cards. cmd_id → original
        # event payload + chosen candidate. Drained by `_handle_user_decision`
        # before normal preview lookup. Map cleared on approve / kill / modify
        # (also on 60s TTL via _pending_previews TTL machinery if applicable).
        self._pending_session_routes: dict[str, dict[str, Any]] = {}
        # UC2 (session browser): the most recent "list my sessions in <project>"
        # result per HUD session_id, so the wearer's follow-up ("continue #2 …")
        # can resolve a numbered pick → resume that archived CC session.
        self._pending_session_browse: dict[str, dict[str, Any]] = {}
        # R-13 / C-55: server-pull-on-demand vision. When router selects a
        # vision-aware tool but `event.payload.image` is None, Cortex emits
        # `request_image` to glass and awaits a matching `image_attached`
        # event. Future keyed by req_id (server-minted); resolved with the
        # image b64 string (or None on timeout/empty payload).
        self._pending_image_requests: dict[str, asyncio.Future[str | None]] = {}
        # cmd_id → { event_id, plan, current_subtask_results, [wake_response_map] }

        # Available tools block for Router prompt + validation set.
        self._tools_block, self._allowed_tools = available_tools_block(
            enabled={
                "echo",
                "applescript_reminders",
                "applescript_calendar",
                "applescript_mail",
                "fs",
                "apple_notes",
                "system_status",
                "apple_shortcuts",
                "twin_query",
                "imessage",
                "safari_state",
                "claude_code",
                "vision_describe",  # Q.4.5 — receives image bytes only when
                                    # router selects it (gated by _VISION_AWARE_TOOLS)
            }
        )

        # HUD session store — file-backed JSONL conversation threads.
        # Each user_invoke either starts a new session or extends an
        # existing one (event.payload.session_id).
        from .sessions import SessionStore
        self.sessions = SessionStore(twin.root)
        # parent_event_id → session_id mapping, used to annotate every
        # progress frame the server emits with the session it belongs to.
        # The client uses this to learn the server-minted session_id for
        # fresh threads (when the user didn't supply one).
        self._event_to_session: dict[str, str] = {}
        # Auto-distiller: background process that watches Modify-with-text
        # decisions and proposes Twin updates when a stable pattern emerges.
        # Hooks in via _handle_user_decision (modify branch).
        from .distiller import Distiller
        self.distiller = Distiller(self)

        # Parse confirm-policies once at construction; reload on Twin write later (Phase 7).
        self._confirm_policies = _parse_confirm_policies(twin.root)

        # Phase 3b — Glass client support.
        # Capabilities of the current Glass connection (set in handle_glass);
        # empty for Console (which uses the existing schema only).
        self._glass_accept: set[str] = set()
        # Per-stream PCM buffer for audio_chunk → whisper pipeline.
        from .audio_buffer import AudioStreamBuffer
        self._audio_buffer = AudioStreamBuffer()
        # Whisper pipeline (lazy — actual model load deferred to first use
        # OR to cortex.main pre-warm call). `small` for finalised utterances;
        # `tiny` for Level-2 streaming partials (faster, less accurate).
        from .whisper_pipeline import WhisperPipeline
        self._whisper = WhisperPipeline(model="small")
        # `base` for partials — ~2-3× faster than `small`, accuracy on short
        # in-flight audio is "good enough" for a streaming preview. Switch to
        # `tiny` if base proves too slow; download with:
        #   bash whisper.cpp/models/download-ggml-model.sh tiny
        self._whisper_partial = WhisperPipeline(model="base")
        # Per-stream guard: while a partial transcription is in flight, skip
        # new partial triggers for that stream so we don't pile up subprocess
        # invocations.
        self._partial_inflight: set[str] = set()
        log.info(
            "confirm_policies.loaded",
            count=len(self._confirm_policies),
            rules=sorted(self._confirm_policies.keys()) if len(self._confirm_policies) < 40 else "<truncated>",
        )

    # ── Glass-side handler ──

    async def handle_glass(self, ws: ServerConnection) -> None:
        # Phase 3b — parse client capabilities from the connect URL query.
        # Glass declares `?accept=hud_state,card,insight,mic_open,mic_close`
        # so we know which glass-shaped frames it wants. Console doesn't
        # pass anything and keeps getting the existing schema.
        accept_kinds: set[str] = set()
        try:
            from urllib.parse import urlparse, parse_qs
            path = getattr(ws, "request", None)
            raw_query = ""
            if path is not None and hasattr(path, "path"):
                raw_query = urlparse(path.path).query or ""
            elif hasattr(ws, "path"):
                raw_query = urlparse(ws.path).query or ""
            if raw_query:
                accept_param = parse_qs(raw_query).get("accept", [""])[0]
                accept_kinds = {k.strip() for k in accept_param.split(",") if k.strip()}
        except Exception as e:
            log.warning("glass.capabilities_parse_failed", error=str(e))
        log.info("glass.connected", remote=ws.remote_address, accept=sorted(accept_kinds))
        self._glass_conn = ws
        self._glass_accept = accept_kinds
        # P2.3 — surface any pending startup card (e.g. TCC denials caught
        # before Glass was online). One-shot; clear after delivery.
        pending_startup = getattr(self, "_pending_startup_card", None)
        if pending_startup:
            try:
                cmd = Command(
                    id=ids.command_id(), ts=datetime.now(timezone.utc),
                    kind="hud_show",
                    payload={
                        "title": pending_startup["title"],
                        "body": pending_startup["body"],
                        "icon": pending_startup.get("icon", "⚠"),
                        "options": [],
                    },
                    requires_confirm=False, ttl_ms=60_000,
                )
                await ws.send(cmd.model_dump_json())
                log.info("startup_card.delivered", title=pending_startup["title"][:60])
            except Exception as e:
                log.warning("startup_card.send_failed", error=str(e))
            self._pending_startup_card = None
        try:
            async for raw in ws:
                event_data = json.loads(raw)
                event_data.pop("id", None)  # Cortex assigns ids on ingress
                event = Event(**event_data, id=ids.event_id())
                log.info("glass.event", id=event.id, kind=event.kind)
                await self._process_event(event)
        except websockets.exceptions.ConnectionClosed:
            log.info("glass.disconnected")
        finally:
            self._glass_conn = None

    async def _process_event(self, event: Event) -> None:
        if self.plane:
            self.plane.record_event(
                event_id=event.id,
                kind=event.kind,
                payload=event.payload or {},
                source="tool_reverse_wake" if event.kind == "tool_reverse_wake" else "glass",
            )
        if event.kind == "user_invoke":
            await self._handle_user_invoke(event)
        elif event.kind == "user_decision":
            await self._handle_user_decision(event)
        elif event.kind == "tool_reverse_wake":
            await self._handle_tool_reverse_wake(event)
        elif event.kind == "agent_progress":
            # CC mid-task event from tool_agent — forward to Glass as non-
            # blocking ticker frame, also keep latest agent metadata so
            # `progress_feedback` knows which tmux session to inject into.
            await self._handle_agent_progress(event)
        elif event.kind == "progress_feedback":
            # Glass-side user input within an agent's progress feedback window
            await self._handle_progress_feedback(event)
        # Phase 3b — Glass client events (audio + voice-fired decision)
        elif event.kind == "audio_chunk":
            await self._handle_audio_chunk(event)
        elif event.kind == "audio_end":
            await self._handle_audio_end(event)
        elif event.kind == "decision_voice":
            await self._handle_decision_voice(event)
        # R-13 / C-55: glass replied to a request_image with the captured frame
        elif event.kind == "image_attached":
            await self._handle_image_attached(event)
        else:
            log.warning("unsupported_event_kind", kind=event.kind)

    # ── v2 agent runtime: progress + feedback ─────────────────────────────

    # Affirmations that match "user said nothing meaningful" — drop silently.
    _AFFIRMATIONS = frozenset({
        "ok", "okay", "k", "kk", "yes", "yep", "yeah", "sure", "go", "go on",
        "continue", "good", "fine", "right", "uh huh",
        "嗯", "嗯嗯", "好", "好的", "可以", "没问题", "行", "对", "对的",
        ".", "..", "...", "",
    })

    @classmethod
    def _is_substantive_feedback(cls, text: str) -> bool:
        """True iff the user's words during a progress window deserve to be
        injected into CC. Empty / affirmations / single-word OK = silent."""
        if not text:
            return False
        t = text.strip().lower()
        # Strip trailing punctuation that affirmations might have
        t = t.rstrip("。，！？!?.,~")
        if not t or t in cls._AFFIRMATIONS:
            return False
        # Very short non-affirmations like "no" / "不" / "wait" / "等" ARE
        # substantive (they redirect). Anything ≥ 2 chars beyond an affirmation
        # is in. Single-char "k" / "." caught by the set above.
        return True

    async def _handle_agent_progress(self, event: Event) -> None:
        """tool_agent pushed an `agent_progress` event. Two jobs:
          - maintain self._active_agents so progress_feedback can route to
            the right CC tmux session
          - forward to Glass as a non-blocking `progress` frame
        """
        payload = event.payload or {}
        parent_event_id = payload.get("parent_event_id")
        stage = payload.get("stage", "?")
        tmux_session = payload.get("tmux_session")
        cc_session_id = payload.get("agent_session_id")

        # Lifecycle: first event registers; completed/error unregister
        if parent_event_id:
            if stage == "started" and tmux_session:
                self._active_agents[parent_event_id] = {
                    "tmux_session": tmux_session,
                    "cc_session_id": cc_session_id,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "last_progress_at": datetime.now(timezone.utc).isoformat(),
                }
            elif stage in ("completed", "error"):
                self._active_agents.pop(parent_event_id, None)
            elif parent_event_id in self._active_agents:
                self._active_agents[parent_event_id]["last_progress_at"] = datetime.now(timezone.utc).isoformat()

        if not self._glass_conn:
            return  # nobody to tell; skip

        # Frame shape — see AGENT-ARCHITECTURE-V2 §3. id starts with "prog_"
        # so the Glass client can distinguish from Command (which uses "cmd_").
        frame = {
            "id": f"prog_{ids.event_id()[4:]}",  # reuse event_id format minus "evt_" prefix
            "kind": "progress",
            "ts": datetime.now(timezone.utc).isoformat(),
            "parent_event_id": parent_event_id,
            "session_id": self._event_to_session.get(parent_event_id or ""),
            "stage": payload.get("stage", "?"),
            "icon": payload.get("icon") or _default_icon_for_stage(payload.get("stage", "?")),
            "detail": payload.get("detail", "")[:200],
            # Cosmetic hints for HUD; client may ignore
            "is_error": bool(payload.get("is_error")),
            "tool": payload.get("tool"),
        }
        try:
            await self._glass_conn.send(json.dumps(frame, ensure_ascii=False))
        except Exception as e:
            log.warning("progress.send_failed", error=str(e))

        # Mirror to the styled `hud_state` flavor so the Glass HUD renders the
        # agent's live action in its replace-in-place Thinking row — the SAME
        # path Cortex-internal steps already use via `_emit_progress_to_glass`
        # (line ~857). Without this mirror the per-CC-tool detail (📖 reading… /
        # 📝 editing… / 🔧 <bash> / 🧵 sub-agent / 💭 thinking) reached the wire
        # as `kind:"progress"` but the Glass client only renders `hud_state`,
        # so the whole agent-activity feed was dropped at the eyewear. Skip
        # stages the wearer doesn't need: tool RESULTS (the "out" — per Zack the
        # HUD shows the action, not bash in/out), and terminal completed/error
        # (a card/insight frame drives the final state; rendering them here just
        # flickers Thinking after the Card).
        if stage not in ("tool_result", "completed", "error") \
                and "hud_state" in self._glass_accept:
            await self.emit_hud_state(
                stage=stage,
                icon=frame["icon"],
                detail_runs=[{"text": (payload.get("detail") or "")[:200], "style": "normal"}],
                meta_runs=[],
            )

    async def _emit_progress_to_glass(
        self,
        *,
        parent_event_id: str,
        stage: str,
        icon: str,
        detail: str,
    ) -> None:
        """Cortex-side internal progress emit. Same wire shape as the
        agent_progress frames from tool_agent, just sourced locally — used to
        make EVERY internal step visible (classifier / selector / brief
        assembly / planner / per-subtask dispatch). The only state that's
        allowed to stay opaque is an LLM in its own thinking phase
        (jsonl-silent CC turn); everything else must surface a real label.
        """
        if not self._glass_conn:
            return
        frame = {
            "id": f"prog_{ids.event_id()[4:]}",
            "kind": "progress",
            "ts": datetime.now(timezone.utc).isoformat(),
            "parent_event_id": parent_event_id,
            "session_id": self._event_to_session.get(parent_event_id or ""),
            "stage": stage,
            "icon": icon,
            "detail": (detail or "")[:200],
            "is_error": False,
            "tool": None,
        }
        try:
            await self._glass_conn.send(json.dumps(frame, ensure_ascii=False))
        except Exception as e:
            log.warning("local_progress.send_failed", error=str(e))

        # Phase 3b — if peer accepts hud_state, also emit the styled-runs
        # flavor. Console drops it (didn't accept); Glass renders into a
        # single replace-in-place row (per design §1.3).
        if "hud_state" in self._glass_accept:
            await self.emit_hud_state(
                stage=stage,
                icon=icon,
                detail_runs=[{"text": detail or "", "style": "normal"}],
                meta_runs=[],
            )

    async def _handle_progress_feedback(self, event: Event) -> None:
        """User typed/spoke something during a progress window. If substantive,
        inject into the active CC tmux session via send-keys. Else drop."""
        payload = event.payload or {}
        parent_event_id = payload.get("in_reply_to_event")
        text = (payload.get("feedback_text") or "").strip()
        if not parent_event_id:
            log.warning("progress_feedback.no_parent")
            return
        active = self._active_agents.get(parent_event_id)
        if not active:
            log.warning("progress_feedback.no_active_agent", parent=parent_event_id)
            return

        if not self._is_substantive_feedback(text):
            log.info("progress_feedback.dropped_as_filler", text=text[:80])
            # Optionally tell Glass that we noted but ignored — saves the user
            # from wondering "did it hear me?"
            if self._glass_conn:
                await self._glass_conn.send(json.dumps({
                    "id": f"prog_{ids.event_id()[4:]}",
                    "kind": "progress",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "parent_event_id": parent_event_id,
                    "stage": "feedback_noted",
                    "icon": "👂",
                    "detail": f"heard \"{text[:40]}\" — continuing",
                }, ensure_ascii=False))
            return

        # Substantive: inject into CC via tmux send-keys + paste-buffer
        tmux_session = active["tmux_session"]
        try:
            await self._inject_feedback_into_agent(tmux_session, text)
            log.info("progress_feedback.injected", parent=parent_event_id, text=text[:80])
            if self._glass_conn:
                await self._glass_conn.send(json.dumps({
                    "id": f"prog_{ids.event_id()[4:]}",
                    "kind": "progress",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "parent_event_id": parent_event_id,
                    "stage": "feedback_injected",
                    "icon": "💬",
                    "detail": f"correction sent: \"{text[:60]}\"",
                }, ensure_ascii=False))
        except Exception as e:
            log.error("progress_feedback.inject_failed", error=str(e), exc_info=True)

    # ── Phase 3b: Glass client — audio + voice-fired decisions ─────────────

    # Level-2 streaming partials: every N chunks of ~250ms (= ~1s of audio)
    # we fire a `tiny` whisper pass on the in-flight buffer and emit a
    # hud_state(stage="listening") with the partial transcript. Set to 4 ×
    # 250ms = 1 second cadence. Drop new triggers while a previous partial
    # is still running for the same stream.
    _PARTIAL_EVERY_N_CHUNKS = 4

    async def _handle_audio_chunk(self, event: Event) -> None:
        """Append a base64 PCM frame to the per-stream buffer; opportunistically
        fire a partial transcription every N chunks (Level 2 streaming)."""
        p = event.payload or {}
        sid = (p.get("stream_id") or "").strip()
        if not sid:
            log.warning("audio_chunk.no_stream_id")
            return
        self._audio_buffer.on_chunk(
            stream_id=sid,
            seq=int(p.get("seq") or 0),
            b64_pcm=p.get("b64_pcm") or "",
            sample_rate=int(p.get("sample_rate") or 16000),
            channels=int(p.get("channels") or 1),
        )

        if "hud_state" not in self._glass_accept:
            return
        entry = self._audio_buffer.peek(sid)
        if entry is None or entry.n_chunks == 0:
            return
        if entry.n_chunks % self._PARTIAL_EVERY_N_CHUNKS != 0:
            return
        if sid in self._partial_inflight:
            log.debug("partial.skip_inflight", stream_id=sid, n_chunks=entry.n_chunks)
            return
        # Snapshot a copy so the running whisper pass doesn't race with new
        # chunks mutating the buffer mid-flight.
        snapshot = bytes(entry.buffer)
        lang_hint = (p.get("lang_hint") or "auto").lower()
        asyncio.create_task(self._run_partial(sid, snapshot, entry.sample_rate, entry.channels, lang_hint))

    async def _run_partial(
        self, stream_id: str, pcm: bytes, sample_rate: int, channels: int, lang: str,
    ) -> None:
        """Run one partial whisper pass and emit a hud_state(listening) frame
        with the current transcript. Best-effort — failures are logged and
        swallowed so the full audio_end pass remains the source of truth."""
        self._partial_inflight.add(stream_id)
        try:
            text = await self._whisper_partial.transcribe(
                pcm, sample_rate=sample_rate, channels=channels,
                lang=lang if lang in ("zh", "en") else "auto",
            )
            text = text.strip()
            if not text:
                return
            log.info("partial.transcript", stream_id=stream_id,
                     n_chars=len(text), bytes=len(pcm))
            await self.emit_hud_state(
                stage="listening",
                icon="🎤",
                detail_runs=[{"text": text, "style": "dim"}],
            )
        except Exception as e:
            log.warning("partial.failed", stream_id=stream_id, error=str(e))
        finally:
            self._partial_inflight.discard(stream_id)

    async def _handle_audio_end(self, event: Event) -> None:
        """Finalize a stream: pop its PCM, run Whisper, inject the transcript
        into the existing classifier pipeline as either a fresh user_invoke
        OR a user_decision.feedback_text (when in CARD modify flow)."""
        p = event.payload or {}
        sid = (p.get("stream_id") or "").strip()
        if not sid:
            log.warning("audio_end.no_stream_id")
            return
        entry = self._audio_buffer.finalize(sid)
        if entry is None or len(entry.buffer) == 0:
            log.warning("audio_end.no_buffer", stream_id=sid)
            return
        # Signal-level probe (diagnosing "whisper gets nothing from N s of audio"):
        # rms≈0 → the captured channel is silent (mic/deinterleave issue); high
        # rms but empty transcript → noise / wrong channel content.
        try:
            import audioop
            _rms = audioop.rms(bytes(entry.buffer), 2)
            _peak = audioop.max(bytes(entry.buffer), 2)
            log.info("audio_end.signal", stream_id=sid, bytes=len(entry.buffer),
                     sample_rate=entry.sample_rate, channels=entry.channels,
                     rms=_rms, peak=_peak)
        except Exception as e:
            log.warning("audio_end.signal_probe_failed", error=str(e))

        lang_hint = (p.get("lang_hint") or "auto").lower()
        intent = (p.get("intent") or "fresh").lower()
        cmd_id = p.get("cmd_id")
        session_id = p.get("session_id")
        # A mic opened FOR a card encodes its purpose + cmd_id in the stream_id
        # (modify_<cmd_id> / answer_<cmd_id>). Derive intent + cmd_id from that so
        # the transcript routes back to the right card instead of falling through
        # to a fresh invoke (the bug that sent question answers to the classifier).
        if sid.startswith("modify_"):
            intent, cmd_id = "modify", cmd_id or sid[len("modify_"):]
        elif sid.startswith("answer_"):
            intent, cmd_id = "answer", cmd_id or sid[len("answer_"):]

        # Bridge: announce we're transcribing (Glass keeps HUD in THINKING
        # while we crunch). Reuses the existing progress channel.
        if self._glass_conn:
            try:
                await self._glass_conn.send(json.dumps({
                    "id": f"prog_{ids.event_id()[4:]}",
                    "kind": "progress",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "stage": "transcribing", "icon": "📝",
                    "detail": f"whisper.cpp · {len(entry.buffer)//1024} kB",
                    "session_id": session_id,
                }, ensure_ascii=False))
            except Exception:
                pass

        try:
            transcript = await self._whisper.transcribe(
                bytes(entry.buffer),
                sample_rate=entry.sample_rate,
                channels=entry.channels,
                lang=lang_hint if lang_hint in ("zh", "en") else "auto",
            )
        except Exception as e:
            log.error("audio_end.transcribe_failed", error=str(e), exc_info=True)
            return

        log.info("audio_end.transcript",
                 stream_id=sid, n_chars=len(transcript), intent=intent)

        if not transcript.strip():
            log.info("audio_end.empty_transcript")
            return

        if intent == "modify" and cmd_id:
            # User said "改 …" → treat as user_decision feedback for the
            # pending card. Synthesize the event the existing handler expects.
            synth = Event(
                id=ids.event_id(),
                kind="user_decision",
                ts=datetime.now(timezone.utc),
                payload={
                    "in_reply_to": cmd_id,
                    "decision": "Modify",
                    "feedback_text": transcript,
                },
            )
            await self._handle_user_decision(synth)
            return

        if intent == "answer" and cmd_id:
            # User spoke their answer to a QUESTION card → feed it as the
            # card's "answer" decision; _handle_user_decision drives CC's
            # AskUserQuestion "Other" with this text.
            synth = Event(
                id=ids.event_id(),
                kind="user_decision",
                ts=datetime.now(timezone.utc),
                payload={
                    "in_reply_to": cmd_id,
                    "decision": "answer",
                    "feedback_text": transcript,
                },
            )
            await self._handle_user_decision(synth)
            return

        # Fresh user_invoke (from IDLE wake or first turn).
        synth = Event(
            id=ids.event_id(),
            kind="user_invoke",
            ts=datetime.now(timezone.utc),
            payload={"text": transcript, "session_id": session_id},
        )
        await self._handle_user_invoke(synth)

    async def _handle_decision_voice(self, event: Event) -> None:
        """InstructSdk on the Glass fired a keyword for the current CARD.
        Convert to the existing user_decision shape and route through the
        existing pipeline so all the receipts / sessions / distiller hooks
        fire identically."""
        p = event.payload or {}
        cmd_id = p.get("cmd_id")
        command = (p.get("command") or "").strip().lower()
        if not cmd_id or not command:
            log.warning("decision_voice.missing_fields", payload=p)
            return
        # Map glass commands → existing 3-button vocabulary.
        # scroll_up / scroll_down are pure HUD ops (we don't forward server-side);
        # 'continue' is a checkpoint advance which the existing approve-on-
        # checkpoint path handles cleanly.
        glass_to_decision = {
            "approve":     "Approve",
            "modify":      "Modify",   # NB: Modify-without-text re-surfaces the card
            "kill":        "Kill",
            "continue":    "Approve",  # checkpoint advance
        }
        decision = glass_to_decision.get(command)
        if decision is None:
            log.info("decision_voice.non_decision_command", command=command)
            return
        log.info("decision_voice.routed", cmd_id=cmd_id, command=command, decision=decision)
        synth = Event(
            id=ids.event_id(),
            kind="user_decision",
            ts=datetime.now(timezone.utc),
            payload={
                "in_reply_to": cmd_id,
                "decision": decision,
                # No feedback_text from voice command — Modify requires a
                # follow-up audio stream (the Glass opens mic locally).
            },
        )
        await self._handle_user_decision(synth)

    # ── Phase 3b: Glass-shaped command emitters ──────────────────────────

    async def _emit_glass_frame(self, kind: str, payload: dict[str, Any]) -> None:
        """Generic emit of a glass-shaped command frame. No-op if the current
        Glass peer didn't declare it in its `?accept=` handshake (i.e., we're
        currently talking to a Console — keep using the existing schema)."""
        if not self._glass_conn or kind not in self._glass_accept:
            return
        frame = {
            "id": ids.command_id(),
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **payload,
        }
        try:
            await self._glass_conn.send(json.dumps(frame, ensure_ascii=False))
            log.info("glass_frame.emit", kind=kind)
        except Exception as e:
            log.warning("glass_frame.emit_failed", kind=kind, error=str(e))

    async def emit_hud_state(
        self, *, stage: str, icon: str | None = None,
        detail_runs: list[dict[str, str]] | None = None,
        meta_runs: list[dict[str, str]] | None = None,
    ) -> None:
        await self._emit_glass_frame("hud_state", {
            "stage": stage,
            "icon": icon,
            "detail_runs": detail_runs or [],
            "meta_runs": meta_runs or [],
        })

    async def emit_card(
        self, *, cmd_id: str,
        title: str, body_md: str,
        scroll_total_lines: int = 0,
        options: list[str] | None = None,
        ttl_ms: int = 30_000,
    ) -> None:
        from .markdown_runs import to_runs
        # `options is None` means "no caller preference — use the canonical
        # approve/modify/kill". An explicit `options=[]` means "info-only card,
        # no actionable buttons" — must be preserved as empty (don't fall
        # through to the truthiness default).
        resolved_options = ["approve", "modify", "kill"] if options is None else options
        await self._emit_glass_frame("card", {
            "cmd_id": cmd_id,
            "title_runs": to_runs(title),
            "body_runs": to_runs(body_md),
            "scroll_total_lines": scroll_total_lines,
            "options": resolved_options,
            # Piece 4: explicit card type so Glass renders + ring-maps cleanly
            # (notification=dismiss · checkpoint=approve/modify/reject · question=answer).
            "card_type": _card_type_for(resolved_options),
            "ttl_ms": ttl_ms,
        })

    async def emit_insight(
        self, *, title: str, body_md: str, insight_kind: str,
        ttl_ms: int = 8_000, context_id: str | None = None,
    ) -> None:
        from .markdown_runs import to_runs
        await self._emit_glass_frame("insight", {
            "title_runs": to_runs(title),
            "body_runs": to_runs(body_md),
            "insight_kind": insight_kind,
            "ttl_ms": ttl_ms,
            "context_id": context_id,
        })

    async def emit_mic_open(
        self, *, stream_id: str, lang_hint: str | None = None,
        ttl_ms: int | None = None,
    ) -> None:
        await self._emit_glass_frame("mic_open", {
            "stream_id": stream_id,
            "lang_hint": lang_hint,
            "ttl_ms": ttl_ms,
        })

    async def emit_mic_close(self, *, stream_id: str) -> None:
        await self._emit_glass_frame("mic_close", {"stream_id": stream_id})

    # ── R-13 / C-55: server-pull-on-demand vision ─────────────────────────

    async def _request_image_from_glass(
        self, parent_event_id: str, hint: str | None = None,
        timeout_s: float = 10.0,
    ) -> str | None:
        """Ask the current glass peer to capture a photo and send it back.
        Returns the b64-encoded JPEG string on success, or None on timeout /
        peer-not-glass / peer-missing-capability.

        Used by `_dispatch_complex_agent` when the router routed to a tool
        in [_VISION_AWARE_TOOLS] but the originating event had no image.
        Glass-side handler: `StateMachine.dispatch` `"request_image"` →
        CameraGate.captureViaGate → `wss.sendEvent(ImageAttached(req_id, b64))`.
        """
        # Only meaningful for glass peers that advertised "card" support
        # (we co-opt the same capability gate — a console peer wouldn't have
        # a camera anyway).
        if not self._glass_conn or "card" not in self._glass_accept:
            return None
        req_id = ids.command_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()
        self._pending_image_requests[req_id] = fut
        payload: dict[str, Any] = {
            "req_id": req_id,
            "parent_event_id": parent_event_id,
        }
        if hint:
            payload["hint"] = hint
        await self._emit_glass_frame("request_image", payload)
        log.info("request_image.sent", req_id=req_id, parent=parent_event_id, hint=hint)
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            log.warning(
                "request_image.timeout",
                req_id=req_id, parent=parent_event_id, timeout_s=timeout_s,
            )
            return None
        finally:
            # Always clear from registry — even on success, the future was
            # set by _handle_image_attached and we're done with this req_id.
            self._pending_image_requests.pop(req_id, None)

    async def _handle_image_attached(self, event: Event) -> None:
        """Glass → Cortex `image_attached` arrives. Resolve the matching
        pending Future so `_request_image_from_glass()` can return."""
        payload = event.payload or {}
        req_id = payload.get("req_id")
        image_b64 = payload.get("image")
        if not req_id:
            log.warning("image_attached.no_req_id", event_id=event.id)
            return
        fut = self._pending_image_requests.get(req_id)
        if fut is None or fut.done():
            log.warning("image_attached.no_pending_or_done", req_id=req_id)
            return
        # Empty string is treated as "tried but no usable image" — None signals
        # the same downstream (fallback to image-less dispatch).
        result: str | None = image_b64 if image_b64 else None
        fut.set_result(result)
        log.info(
            "image_attached.resolved",
            req_id=req_id, has_image=bool(result),
            bytes_b64=len(result) if result else 0,
        )

    async def _send_command(self, cmd: Command) -> None:
        """Unified path: send the existing Command to the Glass peer AND,
        if the peer accepted glass-shaped frames, also emit the styled-runs
        flavor (card/hud_show → card or insight) + mic_open on CARD entry.

        Use this instead of `self._glass_conn.send(cmd.model_dump_json())`
        directly so glass-shaped frames stay in sync with the legacy ones.

        When a glass-shaped frame WILL be emitted below, the legacy Command
        is suppressed: Glass silent-ignores legacy preview_action / hud_show /
        tool_card since P1.6c (edfb3ba), so double-emit is pure overhead
        (~30-40% of CARD traffic). Skipping it on every CARD halves per-card
        WSS bytes with zero downstream impact. Legacy still goes out for kinds
        that have no glass-shaped equivalent."""
        if not self._glass_conn:
            return
        # Pre-decide whether a glass-shaped frame will follow.
        insight_kind = cmd.payload.get("_insight_kind") if cmd.kind == "hud_show" else None
        will_emit_glass = (
            (cmd.kind == "preview_action" and "card" in self._glass_accept)
            or (cmd.kind == "hud_show" and bool(insight_kind) and "insight" in self._glass_accept)
            or (cmd.kind == "hud_show" and not insight_kind and "card" in self._glass_accept)
        )
        if not will_emit_glass:
            try:
                await self._glass_conn.send(cmd.model_dump_json())
            except Exception as e:
                log.warning("command.send_failed", id=cmd.id, kind=cmd.kind, error=str(e))
                return
        # Glass-shaped frame, if the peer wants one.
        if cmd.kind == "preview_action":
            options = cmd.payload.get("options") or []
            await self.emit_card(
                cmd_id=cmd.id,
                title=cmd.payload.get("title", ""),
                body_md=cmd.payload.get("body", ""),
                options=options,
                ttl_ms=cmd.ttl_ms,
            )
            # NB: the mic is NOT opened here. Under ring-exclusive control the
            # mic opens only when the wearer explicitly asks to Modify (ring
            # LONG_PRESS → user_decision modify-without-text → emit_mic_open in
            # `_handle_user_decision`). Opening it speculatively on every
            # actionable card held the mic for 30s per card — wasteful and
            # against C-37 energy-first.
        # hud_show fan-out for glass peers:
        #   - has `_insight_kind` marker + peer accepts `insight` → glass insight
        #     frame (proactive surface, TTL countdown, no buttons)
        #   - otherwise + peer accepts `card`                     → glass card
        #     frame with options=[] (info-only response: user reads the body,
        #     no approve/modify/kill — fixes P1.6b protocol gap where pure-info
        #     hud_show responses were dropped on the glass side)
        elif cmd.kind == "hud_show":
            insight_kind = cmd.payload.get("_insight_kind")
            if insight_kind and "insight" in self._glass_accept:
                await self.emit_insight(
                    title=cmd.payload.get("title", ""),
                    body_md=cmd.payload.get("body", ""),
                    insight_kind=insight_kind,
                    ttl_ms=cmd.ttl_ms,
                )
            elif "card" in self._glass_accept:
                await self.emit_card(
                    cmd_id=cmd.id,
                    title=cmd.payload.get("title", ""),
                    body_md=cmd.payload.get("body", ""),
                    options=[],  # info-only: caller relies on emit_card preserving []
                    ttl_ms=cmd.ttl_ms,
                )

    # ── P0.1: HUD-session-scoped CC tmux registry ─────────────────────────
    # TTL: a tmux idle for more than this is considered stale and gets
    # evicted on the next reuse attempt (cold-start preferred over an
    # attention-diluted long-running CC session). 30 min is plenty for an
    # active conversation; longer and the user is likely on a new topic.
    _HUD_TMUX_TTL_S = 1800.0

    def _hud_tmux_register(
        self, session_id: str | None, agent_result: dict[str, Any],
        *, working_dir: str | None, timeout_s: float,
        ask_text: str | None = None,
    ) -> None:
        """Stash the (tmux, cc_session_id) so the next invoke in the same
        HUD session can reuse it via agent_continue. Caller must have
        dispatched the agent with keep_alive_on_final=True.

        R-14: extended metadata used by `_route_session` to disambiguate
        when the user voice-references "the email one" / "auth refactor".
        `title` is preserved across turns; `turn_count` increments; other
        fields refreshed each turn.
        """
        if not session_id:
            return
        tmux = agent_result.get("tmux_session")
        cc_sid = agent_result.get("session_id")
        if not tmux or not cc_sid:
            return
        now = time.time()
        existing = self._active_hud_session_tmux.get(session_id)
        new_summary = (
            ((agent_result.get("structured") or {}).get("summary") or "")[:120]
            if isinstance(agent_result.get("structured"), dict) else ""
        )
        # `title` is sticky — derived from the first prompt or summary; subsequent
        # turns keep the original (so voice references like "the email one" still
        # match even after many turns of follow-up). When neither ask_text nor a
        # structured summary is available (agent returned plain text), fall back
        # to the SessionStore-derived title (first turn's ask_text) so pin /
        # confirmation cards never show "(untitled)".
        if existing and existing.get("title"):
            title = existing["title"]
        else:
            title = _derive_session_title(ask_text or new_summary)
            if not title or title == "(untitled)":
                title = self.sessions.title_for(session_id)
        self._active_hud_session_tmux[session_id] = {
            "tmux_session": tmux,
            "cc_session_id": cc_sid,
            "working_dir": working_dir,
            "timeout_s": float(timeout_s),
            "last_activity": now,
            "last_summary": new_summary,
            # ── R-14 additions ──
            "title": title,
            "created_at": (existing.get("created_at") if existing else now),
            "turn_count": (int(existing.get("turn_count", 0)) if existing else 0) + 1,
            "pinned": bool(existing.get("pinned")) if existing else False,
        }
        log.info(
            "hud_tmux.registered",
            session_id=session_id, tmux=tmux, cc_sid=cc_sid[:8],
            title=title, turn=self._active_hud_session_tmux[session_id]["turn_count"],
        )

    async def _hud_tmux_evict(self, session_id: str, *, reason: str) -> None:
        """Drop the entry and kill its tmux. Used on Kill, TTL eviction, or
        when reuse fails (tmux gone, jsonl missing)."""
        entry = self._active_hud_session_tmux.pop(session_id, None)
        if not entry:
            return
        tmux = entry.get("tmux_session")
        log.info("hud_tmux.evict", session_id=session_id, tmux=tmux, reason=reason)
        if not tmux:
            return
        try:
            await self._dispatch_to_tool({
                "tool": "claude_code", "action": "agent_kill",
                "args": {"tmux_session": tmux},
                "result_format": "execute",
            })
        except Exception as e:
            log.warning("hud_tmux.kill_failed", error=str(e), tmux=tmux)

    def _hud_tmux_lookup(self, session_id: str | None) -> dict[str, Any] | None:
        """Return the fresh entry, or None if absent / stale.

        R-14.b: pinned sessions skip the TTL stale check — they live until
        explicitly unpinned or Kill'd.
        """
        if not session_id:
            return None
        entry = self._active_hud_session_tmux.get(session_id)
        if not entry:
            return None
        if entry.get("pinned"):
            return entry  # pinned → never stale
        if (time.time() - float(entry.get("last_activity") or 0)) > self._HUD_TMUX_TTL_S:
            log.info(
                "hud_tmux.stale_skipping",
                session_id=session_id, tmux=entry.get("tmux_session"),
            )
            return None
        return entry

    async def _handle_pin_command(
        self, event: Event, existing_sid: str | None, ask_text: str,
    ) -> None:
        """R-14.b: handle whole-utterance voice pin/unpin commands. Short-
        circuits classifier+dispatch. Emits a brief info-only CARD with the
        result. If no session is active (or sid not registered), emit a
        friendly no-op card so the user gets feedback instead of silence.

        Routing for pin/unpin:
          - If existing_sid is set and live → operate on that session
          - Else if there's exactly 1 active session → operate on it
          - Else → emit "no active session" card; no-op
        """
        is_pin = _looks_pin_intent(ask_text)  # else: unpin
        # Resolve target
        entry = self._active_hud_session_tmux.get(existing_sid) if existing_sid else None
        if not entry:
            active = [s for s in self._gather_active_sessions()]
            if len(active) == 1:
                target_sid = active[0]["session_id"]
                entry = self._active_hud_session_tmux.get(target_sid)
        else:
            target_sid = existing_sid

        if not entry:
            # No session to act on — give the user a quick info card so the
            # voice command doesn't appear to be ignored.
            await self._emit_info_card(
                event_id=event.id,
                title="No active session",
                body=("No session to pin." if is_pin else "No session to unpin."),
                ttl_ms=5_000,
            )
            return

        title = entry.get("title") or "(untitled)"
        if is_pin:
            entry["pinned"] = True
            log.info("session.pinned", session_id=target_sid, title=title)
            confirm_title = "📌 Pinned"
            body = f"'{title}' stays alive until you unpin or kill it."
        else:
            entry["pinned"] = False
            log.info("session.unpinned", session_id=target_sid, title=title)
            confirm_title = "📌 Unpinned"
            body = f"'{title}' is back on the 30-min TTL."
        await self._emit_info_card(
            event_id=event.id,
            title=confirm_title,
            body=body,
            ttl_ms=5_000,
        )

    async def _emit_info_card(
        self, *, event_id: str, title: str, body: str, ttl_ms: int = 5_000,
    ) -> None:
        """Lightweight info-only card (no options) used for confirmation
        feedback. Goes through `emit_card` (with options=[]) so the glass-side
        info-only TTL + double-click dismiss machinery kicks in (R-13 / C-55
        Issue 1 fix preserves empty-list passthrough). Falls back to no-op
        silently when no glass peer is connected."""
        await self.emit_card(
            cmd_id=ids.command_id(),
            title=title,
            body_md=body,
            options=[],
            ttl_ms=ttl_ms,
        )

    async def _handle_shortcut_config(self, event: Event, ask_text: str) -> None:
        """Voice-driven shortcut-slot edit. Parses "set shortcut N to …" into a
        slot update and pushes a `shortcut_config` frame to Glass (which owns
        the local slot store), then a confirmation card. Short-circuits the
        normal classifier/dispatch — this edits config, it doesn't run a task."""
        from .shortcut_config import parse_shortcut_config
        cfg = await parse_shortcut_config(ask_text)
        if cfg is None:
            await self._emit_info_card(
                event_id=event.id,
                title="Shortcut unchanged",
                body="Couldn't parse that. Try: \"set shortcut 2 to ask what's in front\".",
                ttl_ms=6_000,
            )
            return
        # Push the slot update to Glass (slot content lives app-side).
        await self._emit_glass_frame("shortcut_config", {
            "slot": cfg["slot"],
            "prompt": cfg["prompt"],
            "send_photo": cfg["send_photo"],
            "label": cfg["label"],
        })
        log.info(
            "shortcut_config.emitted",
            slot=cfg["slot"], send_photo=cfg["send_photo"], label=cfg["label"],
        )
        photo = "📷 + " if cfg["send_photo"] else ""
        await self._emit_info_card(
            event_id=event.id,
            title=f"Shortcut {cfg['slot']} set",
            body=f"{photo}\"{cfg['prompt']}\"",
            ttl_ms=6_000,
        )

    async def _handle_session_browse(
        self, event: Event, ask_text: str, sid: str | None,
    ) -> None:
        """UC2 (a): "list my recent sessions in <project>". Deterministic disk
        read (no LLM) → a numbered info card. Stash the listing keyed by the HUD
        session_id so the wearer's next utterance ("continue #2 …") can resolve a
        pick. The card also shows each session's last user message ("what did I
        say last") so that question is answered in-line."""
        from .session_browser import extract_project_query, list_project_sessions

        project = extract_project_query(ask_text)
        result = await asyncio.to_thread(list_project_sessions, project, 5)
        sessions = result.get("sessions") or []
        if not sessions:
            await self._emit_info_card(
                event_id=event.id,
                title="No sessions found",
                body=f"No Claude sessions matched “{project or 'that project'}”.",
                ttl_ms=8_000,
            )
            return

        # Stash for the pick follow-up (keyed by current HUD session; fall back
        # to a sentinel so a session-less test invoke can still pick).
        self._pending_session_browse[sid or "_no_sid"] = {
            "sessions": sessions,
            "project": result.get("matched_bucket"),
            "ts": time.time(),
        }

        lines = []
        for i, s in enumerate(sessions, 1):
            last = s.get("last_user_msg") or ""
            lines.append(f"{i}. {s['title'][:46]}  ({s['n_user_turns']}t·{s['age_min']}m)")
            if last:
                lines.append(f"   ↪ you: “{last[:60]}”")
        body = "\n".join(lines) + "\n\nSay “continue #N: <instruction>”."
        await self._emit_info_card(
            event_id=event.id,
            title=f"🗂 {result.get('working_dir','').split('/')[-1] or 'sessions'} — recent",
            body=body,
            ttl_ms=30_000,
        )
        log.info("session_browse.listed", project=project,
                 bucket=result.get("matched_bucket"), n=len(sessions))

    async def _handle_session_browse_pick(
        self, event: Event, pending: dict[str, Any], idx: int, instruction: str,
    ) -> None:
        """UC2 (b): the wearer picked session #idx from a prior listing and (maybe)
        gave a new instruction. Resume that ARCHIVED CC session via
        `claude_code.agent` + `resume_cc_session_id` (the same path
        `_resume_agent_with_modify`'s fallback uses), running in the session's own
        cwd. If no instruction was given, just confirm entry and wait for the
        next turn. Registers the resumed session in the HUD-tmux map so further
        turns continue it via agent_continue."""
        from .session_browser import working_dir_for_session
        from .agent_brief import CANONICAL_ACTIONS_SCHEMA

        from .session_browser import is_session_live

        sessions = pending.get("sessions") or []
        if not (1 <= idx <= len(sessions)):
            return
        picked = sessions[idx - 1]
        cc_sid = picked["session_id"]
        working_dir = working_dir_for_session(cc_sid) or picked.get("working_dir")
        log.info("session_browse.picked", idx=idx, cc_sid=cc_sid[:8],
                 title=(picked.get("title") or "")[:40],
                 has_instruction=bool(instruction))
        # Consume the pending listing (one pick per listing).
        self._pending_session_browse.pop(event.payload.get("session_id") or "_no_sid", None)

        # UC2: never resume a LIVE session (open in VS Code etc.) — a second
        # `claude --resume` on the same jsonl forks/corrupts it. Gate on a FRESH
        # re-check (the listing's `live` flag can be stale by pick time, and is
        # used only for the card's 🔴 marker). Live → browse-only.
        if is_session_live(cc_sid):
            await self._emit_info_card(
                event_id=event.id,
                title=f"🔴 {picked['title'][:38]} (live)",
                body=(f"This session is open elsewhere (VS Code) — I can't continue it "
                      f"from here without conflicting.\nLast message:\n“{picked.get('last_user_msg','')[:120]}”"),
                ttl_ms=20_000,
            )
            log.info("session_browse.pick_live_blocked", cc_sid=cc_sid[:8])
            return

        if not instruction:
            # Pick-only: enter the session, surface its last message, await next turn.
            await self._emit_info_card(
                event_id=event.id,
                title=f"▶ {picked['title'][:40]}",
                body=(f"Resuming this session.\nYour last message:\n“{picked.get('last_user_msg','')[:120]}”"
                      "\n\nWhat should I tell it?"),
                ttl_ms=20_000,
            )
            # Remember the target so a plain next utterance continues it. We model
            # this as a 1-item pending pick that any non-pick utterance resolves.
            self._pending_session_browse[event.payload.get("session_id") or "_no_sid"] = {
                "sessions": [picked], "project": pending.get("project"),
                "ts": time.time(), "await_instruction": True,
            }
            return

        await self._emit_progress_to_glass(
            parent_event_id=event.id,
            stage="resuming_agent", icon="▶️",
            detail=f"resuming “{picked['title'][:30]}”",
        )
        add_dirs = [
            os.path.expanduser("~/constellation/twin"),
            os.path.expanduser("~/Code/Projects"),
            os.path.expanduser("~/.claude/projects"),
        ]
        try:
            rpc = await self._dispatch_to_tool({
                "tool": "claude_code", "action": "agent",
                "args": {
                    "brief": instruction,
                    "output_schema_hint": CANONICAL_ACTIONS_SCHEMA,
                    "add_dirs": add_dirs,
                    "working_dir": working_dir,
                    "parent_event_id": event.id,
                    "timeout_s": 240.0,
                    "resume_cc_session_id": cc_sid,
                    "keep_alive_on_final": True,
                },
                "result_format": "execute",
            })
        except Exception as e:
            log.error("session_browse.resume_failed", error=str(e), cc_sid=cc_sid[:8])
            await self._emit_info_card(
                event_id=event.id, title="Couldn't resume",
                body=f"Resume of that session failed: {e}", ttl_ms=8_000,
            )
            return

        rpc_result = rpc.result or {}
        if rpc_result.get("resume_failed") or not rpc_result.get("ok", True):
            await self._emit_info_card(
                event_id=event.id, title="Couldn't resume",
                body=f"That session couldn't be rehydrated ({rpc_result.get('error','unknown')}).",
                ttl_ms=8_000,
            )
            return
        # Register so subsequent turns continue this (now-live) session.
        new_sid = event.payload.get("session_id")
        if new_sid:
            self._hud_tmux_register(
                new_sid, rpc_result, working_dir=working_dir,
                timeout_s=240.0, ask_text=picked["title"],
            )
        await self._send_agent_card_for_decision(
            rpc_result, event, working_dir, 240.0,
        )
        log.info("session_browse.resumed", cc_sid=cc_sid[:8],
                 instruction_preview=instruction[:60])

    async def _emit_session_route_confirmation(
        self, *, event: Event, candidate_session_id: str,
        candidate_title: str, candidate_summary: str, confidence: float,
    ) -> None:
        """R-14.c: when session_router decides "continue" with medium
        confidence (0.4-0.7), ask the user via an actionable CARD whether
        to switch into the candidate session. Approve → route there;
        Modify → for v1 same as Kill (start new); Kill → start new.

        The original event is held in `_pending_session_routes[cmd_id]` until
        the user's decision arrives, then re-injected via `_handle_user_invoke`
        with `_skip_session_router=True` so the router doesn't loop.
        """
        cmd_id = ids.command_id()
        body = (
            f"Continue '{candidate_title}'?\n"
            + (f"  ↪ {candidate_summary[:80]}\n" if candidate_summary else "")
            + "\nClick=yes  ·  Long=clarify  ·  Double=new session"
        )
        # Save the original event so we can re-inject it after the user picks.
        # Copy payload because we'll mutate it on re-injection.
        self._pending_session_routes[cmd_id] = {
            "event_id": event.id,
            "original_payload": dict(event.payload or {}),
            "candidate_session_id": candidate_session_id,
            "candidate_title": candidate_title,
            "confidence": confidence,
            "ts": time.time(),
        }
        await self.emit_card(
            cmd_id=cmd_id,
            title="🧭 Continue this session?",
            body_md=body,
            options=["approve", "modify", "kill"],
            ttl_ms=30_000,
        )
        log.info(
            "session_router.confirmation_card.emitted",
            cmd_id=cmd_id, candidate=candidate_session_id,
            title=candidate_title, confidence=confidence,
        )

    async def _resolve_session_route_decision(
        self, route: dict[str, Any], decision_kind: str,
    ) -> None:
        """R-14.c: user picked from the session-route confirmation card.
        Re-injects the original user_invoke event with the chosen
        session_id pre-set and `_skip_session_router=True` so we don't loop.
        - approve → use route["candidate_session_id"]
        - kill    → use None (start new)
        - modify  → v1: same as kill (start new). TODO: re-run router with
                    this candidate excluded, or accept new voice input.
        """
        new_payload = dict(route["original_payload"])
        if decision_kind == "approve":
            new_payload["session_id"] = route["candidate_session_id"]
            log.info(
                "session_router.confirmed",
                candidate=route["candidate_session_id"],
                title=route.get("candidate_title"),
            )
        else:  # kill or modify
            new_payload.pop("session_id", None)
            log.info(
                "session_router.declined",
                decision=decision_kind,
                candidate=route["candidate_session_id"],
            )
        new_payload["_skip_session_router"] = True
        new_event = Event(
            id=ids.event_id(),
            kind="user_invoke",
            ts=datetime.now(timezone.utc),
            payload=new_payload,
        )
        await self._handle_user_invoke(new_event)

    def _gather_active_sessions(self) -> list[dict[str, Any]]:
        """R-14 / C-56: snapshot of currently-active (non-stale) HUD sessions,
        sorted most-recent-first. Used by session_router to disambiguate
        user voice references like "the email one" / "auth refactor".

        Each item has the keys session_router expects: session_id, title,
        last_summary, last_activity, turn_count, created_at.
        """
        now = time.time()
        out: list[dict[str, Any]] = []
        for sid, entry in self._active_hud_session_tmux.items():
            last_activity = float(entry.get("last_activity") or 0)
            if (now - last_activity) > self._HUD_TMUX_TTL_S:
                continue  # stale — sweeper will evict on its next pass
            out.append({
                "session_id": sid,
                "title": entry.get("title") or "(untitled)",
                "last_summary": entry.get("last_summary") or "",
                "last_activity": last_activity,
                "turn_count": int(entry.get("turn_count", 0)),
                "created_at": float(entry.get("created_at") or last_activity),
            })
        out.sort(key=lambda s: s["last_activity"], reverse=True)
        return out

    # P0.1 patch — proactive TTL eviction. Without this, a user who fires
    # one invoke then walks away leaves the tmux alive forever (lazy lookup
    # only fires on the next invoke in the same session). Sweeper scans
    # every 5 min and kills entries past TTL.
    _HUD_TMUX_SWEEPER_INTERVAL_S = 300.0

    async def _hud_tmux_sweeper_loop(self) -> None:
        """Background task. Periodically evicts stale tmux entries so they
        don't pile up across days of run-time."""
        while True:
            try:
                await asyncio.sleep(self._HUD_TMUX_SWEEPER_INTERVAL_S)
            except asyncio.CancelledError:
                return
            try:
                now = time.time()
                stale: list[str] = []
                n_pinned_skipped = 0
                for sid, entry in list(self._active_hud_session_tmux.items()):
                    if entry.get("pinned"):
                        n_pinned_skipped += 1
                        continue  # R-14.b: pinned sessions never auto-evict
                    age = now - float(entry.get("last_activity") or 0)
                    if age > self._HUD_TMUX_TTL_S:
                        stale.append(sid)
                if stale or n_pinned_skipped:
                    log.info(
                        "hud_tmux.sweep",
                        n_stale=len(stale), n_pinned_skipped=n_pinned_skipped,
                    )
                for sid in stale:
                    await self._hud_tmux_evict(sid, reason="ttl_sweeper")
                # R-14.c: drop confirmation-card entries the user never decided
                # on. Card TTL is 30s; 2× that gives safe margin before reclaim.
                stale_routes = [
                    cid for cid, r in self._pending_session_routes.items()
                    if (now - float(r.get("ts") or 0)) > 60.0
                ]
                for cid in stale_routes:
                    self._pending_session_routes.pop(cid, None)
                if stale_routes:
                    log.info("pending_session_routes.swept", n=len(stale_routes))
            except Exception as e:
                log.warning("hud_tmux.sweep_failed", error=str(e), exc_info=True)

    async def _resume_agent_with_modify(
        self, pending: dict[str, Any], modify_text: str, event: Event,
    ) -> None:
        """Modify-on-FINAL routing (Zack 2026-05-25 v3, P0.1 update):

        Preferred path: a live tmux exists for this HUD session (because
        we now keep tmux alive on FINAL). Paste the user's modify text via
        agent_continue — no spawn cost, no --resume jsonl scan.

        Fallback path: no live tmux (TTL evicted, or pre-P0.1 state) —
        spawn a fresh CC with `--resume <prior cc_session_id>`. CC loads
        its own jsonl on startup, our brief becomes the next user turn,
        and CC produces a revised actions[] without losing research.

        Failure of either path → raise ResumeFailed; caller falls back to
        `_replan_with_feedback` (one-shot re-plan via Router).
        """
        from .agent_brief import build_modify_brief, CANONICAL_ACTIONS_SCHEMA

        agent_result = pending["agent_result"] or {}
        cc_session_id = agent_result.get("session_id")
        working_dir = pending.get("agent_working_dir")
        timeout_s = float(pending.get("agent_timeout_s") or 240)
        original_event = pending["event"]
        prior_struct = pending.get("agent_structured") or {}

        if not cc_session_id:
            raise ResumeFailed("no cc_session_id on pending — can't resume")

        sid = pending.get("session_id")
        if sid:
            self.sessions.append(
                sid, "modify_resume",
                cc_session_id=cc_session_id, modify_text=modify_text[:300],
            )

        # ── P0.1 preferred path: paste into live tmux via agent_continue ──
        reuse_entry = self._hud_tmux_lookup(sid) if sid else None
        if reuse_entry and reuse_entry.get("cc_session_id") == cc_session_id:
            await self._emit_progress_to_glass(
                parent_event_id=original_event.id,
                stage="resuming_agent", icon="✍️",
                detail=f"redirecting live CC session {cc_session_id[:8]} (no respawn)",
            )
            try:
                rpc = await self._dispatch_to_tool({
                    "tool": "claude_code", "action": "agent_continue",
                    "args": {
                        "tmux_session": reuse_entry["tmux_session"],
                        "cc_session_id": cc_session_id,
                        "user_text": modify_text,
                        "working_dir": reuse_entry.get("working_dir") or working_dir,
                        "parent_event_id": original_event.id,
                        "timeout_s": timeout_s,
                        "keep_alive_on_final": True,
                    },
                    "result_format": "execute",
                })
            except Exception as e:
                log.warning("modify_resume.continue_failed", error=str(e), exc_info=True)
                await self._hud_tmux_evict(sid, reason="modify_continue_failed")
                reuse_entry = None
            else:
                rpc_result = rpc.result or {}
                if rpc_result.get("error") or not rpc_result.get("ok"):
                    log.warning(
                        "modify_resume.continue_returned_error",
                        error=rpc_result.get("error"),
                    )
                    await self._hud_tmux_evict(sid, reason="modify_continue_error")
                    reuse_entry = None
                else:
                    self._hud_tmux_register(
                        sid, rpc_result,
                        working_dir=reuse_entry.get("working_dir") or working_dir,
                        timeout_s=timeout_s,
                    )
                    await self._send_agent_card_for_decision(
                        rpc_result, original_event,
                        reuse_entry.get("working_dir") or working_dir,
                        timeout_s,
                    )
                    return

        # ── Fallback path: tmux gone, do the --resume spawn ──
        await self._emit_progress_to_glass(
            parent_event_id=original_event.id,
            stage="resuming_agent", icon="✍️",
            detail=f"resuming CC session {cc_session_id[:8]} with your correction",
        )

        brief = build_modify_brief(
            prior_summary=prior_struct.get("summary"),
            prior_actions=prior_struct.get("actions"),
            prior_notes=prior_struct.get("notes"),
            modify_text=modify_text,
            now_iso=datetime.now(timezone.utc).astimezone().isoformat(),
        )

        add_dirs = [
            os.path.expanduser("~/constellation/twin"),
            os.path.expanduser("~/Code/Projects"),
            os.path.expanduser("~/.claude/projects"),
        ]

        try:
            rpc = await self._dispatch_to_tool({
                "tool": "claude_code", "action": "agent",
                "args": {
                    "brief": brief,
                    "output_schema_hint": CANONICAL_ACTIONS_SCHEMA,
                    "add_dirs": add_dirs,
                    "working_dir": working_dir,
                    "parent_event_id": original_event.id,
                    "timeout_s": timeout_s,
                    "resume_cc_session_id": cc_session_id,
                    "keep_alive_on_final": True,
                },
                "result_format": "execute",
            })
        except Exception as e:
            log.error("modify_resume.dispatch_failed", error=str(e), exc_info=True)
            raise

        rpc_result = rpc.result or {}
        # If resume failed (jsonl gone, CC crashed at spawn) → raise so
        # the caller falls back to _replan_with_feedback.
        if rpc_result.get("resume_failed"):
            raise ResumeFailed(
                f"prior CC session {cc_session_id} could not be resumed "
                f"(jsonl missing or CC spawn failed)"
            )

        if sid:
            self._hud_tmux_register(
                sid, rpc_result,
                working_dir=working_dir, timeout_s=timeout_s,
            )

        await self._send_agent_card_for_decision(rpc_result, original_event, working_dir, timeout_s)

    async def _resume_agent_phase(
        self, pending: dict[str, Any], decision: str, feedback_text: str | None, event: Event,
    ) -> None:
        """Continue a paused multi-phase agent. Per Zack 2026-05-25 the
        decision surface is reduced to exactly two outcomes:

          - APPROVE: send the literal "continue" to CC; it follows `next:`
            as proposed.
          - MODIFY:  MUST carry text. Send the user's text to CC as a fresh
            user message; CC integrates and re-emits.

        A clicked-Modify with NO text is a NO-OP — we re-surface the same
        card so the user can supply text. (Old bug: empty Modify silently
        became continue, swallowing the user's intent.)
        """
        agent_result = pending["agent_result"]
        tmux_session = agent_result.get("tmux_session")
        cc_session_id = agent_result.get("session_id")
        working_dir = pending.get("agent_working_dir")
        timeout_s = float(pending.get("agent_timeout_s") or 240)
        original_event = pending["event"]

        # Caller (_handle_user_decision) has already canonicalized to
        # approve | modify-with-text; the modify-without-text case never
        # reaches us (parent re-surfaces the card and bails).
        kind, resolved_text = _classify_user_decision(decision, feedback_text)
        user_text = "continue" if kind == "approve" else (resolved_text or "continue")

        log.info("agent.resuming", tmux_session=tmux_session, user_text=user_text[:80])

        try:
            rpc = await self._dispatch_to_tool({
                "tool": "claude_code", "action": "agent_continue",
                "args": {
                    "tmux_session": tmux_session,
                    "cc_session_id": cc_session_id,
                    "user_text": user_text,
                    "working_dir": working_dir,
                    "parent_event_id": original_event.id,
                    "timeout_s": timeout_s,
                    "keep_alive_on_final": True,
                },
                "result_format": "execute",
            })
        except Exception as e:
            log.error("agent_continue.failed", error=str(e), exc_info=True)
            return

        # Build + send the next card (could be checkpoint or final)
        # We re-use the same helper as the initial dispatch; need to import
        # locally to avoid a circular reference at module-import time.
        from .http import make_app  # noqa: F401 — ensures the helper module is loaded
        # The helper lives inside make_app's closure, so we recreate the logic
        # here directly rather than digging through closure vars. Cheap dup.
        rpc_result = rpc.result or {}
        # Refresh the HUD-session registry — both checkpoint and final
        # keep tmux alive now, so the next turn can paste into it.
        sid = pending.get("session_id")
        if sid:
            self._hud_tmux_register(
                sid, rpc_result, working_dir=working_dir, timeout_s=timeout_s,
            )
        await self._send_agent_card_for_decision(rpc_result, original_event, working_dir, timeout_s)

    async def _send_agent_card_for_decision(
        self, rpc_result: dict, event: Event, working_dir: str | None, timeout_s: float,
    ) -> None:
        """Same card logic as http._send_agent_card. Kept here so the
        user_decision path doesn't need to round-trip through http module."""
        from .schema import Command
        structured = rpc_result.get("structured") if isinstance(rpc_result.get("structured"), dict) else None
        is_checkpoint = _is_checkpoint(structured) or bool(rpc_result.get("is_checkpoint"))

        if is_checkpoint and structured is not None:
            summary = (structured.get("summary") or "phase done").strip()
            found = (structured.get("found") or "").strip()
            nxt = (structured.get("next") or "").strip()
            lines = [f"**⏸ Phase done:** {summary}"]
            if found:
                lines += ["", found]
            lines += ["", f"**Next:** {nxt}"]
            cmd = Command(
                id=ids.command_id(), ts=datetime.now(timezone.utc),
                kind="preview_action",
                payload={
                    "title": f"Phase pause — {summary[:60]}",
                    "body": "\n".join(lines)[:2000],
                    "icon": "⏸",
                    "options": list(_THREE_OPTIONS),
                },
                requires_confirm=True, ttl_ms=600_000,
            )
            sid = (event.payload or {}).get("session_id")
            self._pending_previews[cmd.id] = {
                "event": event,
                "session_id": sid,
                "plan": {
                    "primary_intent": "agent_checkpoint",
                    "subtasks": [], "reasoning": "phase checkpoint",
                    "hud_response": cmd.payload,
                },
                "subtask_results": [],
                "from_agent": True, "is_checkpoint": True,
                "agent_result": rpc_result,
                "agent_working_dir": working_dir,
                "agent_timeout_s": timeout_s,
            }
            if sid:
                self.sessions.append(
                    sid, "card_surfaced",
                    cmd_id=cmd.id, cmd_kind="preview_action", is_checkpoint=True,
                    title=cmd.payload["title"], body_excerpt=cmd.payload["body"][:400],
                )
            await self._send_command(cmd)
            return

        # FINAL
        actions = (structured or {}).get("actions") if structured else None
        if isinstance(actions, list) and actions:
            subtasks = [_action_to_subtask(a) for a in actions]
            subtasks = [s for s in subtasks if s is not None]
            body_md = _render_actions_preview(
                actions, summary=structured.get("summary"), notes=structured.get("notes"),
            )
            title = f"Agent ready — {len(subtasks)} action{'s' if len(subtasks) != 1 else ''}"
        else:
            subtasks = []
            # Prefer the structured `summary` field (clean prose CC was asked
            # to produce) over the raw result_text (which is the whole JSON
            # blob {summary, actions, notes} serialized — looks like raw JSON
            # in the HUD body). Notes append if present, for context.
            summary = (structured or {}).get("summary") if isinstance(structured, dict) else None
            notes = (structured or {}).get("notes") if isinstance(structured, dict) else None
            if summary:
                body_md = summary if not notes else f"{summary}\n\n— {notes}"
            else:
                body_md = (rpc_result.get("result_text") or "(no actions proposed)")
            body_md = body_md[:1500]
            title = "Agent finished — no actions"

        cmd = Command(
            id=ids.command_id(), ts=datetime.now(timezone.utc),
            kind="preview_action",
            payload={
                "title": title, "body": body_md[:2000], "icon": "✦",
                "options": (list(_THREE_OPTIONS) if subtasks else []),
            },
            requires_confirm=bool(subtasks), ttl_ms=300_000,
        )
        sid = (event.payload or {}).get("session_id")
        self._pending_previews[cmd.id] = {
            "event": event,
            "session_id": sid,
            "plan": {
                "primary_intent": "agent_actions",
                "subtasks": subtasks, "reasoning": "agent dispatch (resumed)",
                "hud_response": cmd.payload,
            },
            "subtask_results": [{} for _ in subtasks],
            "from_agent": True,
            # from_agent_final routes Modify back through CC via --session-id
            # resume (instead of falling through to v0.5 planner). Carries
            # the structured output so build_modify_brief can show CC what
            # it previously proposed.
            "from_agent_final": True,
            "agent_result": rpc_result,
            "agent_structured": (rpc_result or {}).get("structured"),
            "agent_working_dir": working_dir,
            "agent_timeout_s": timeout_s,
        }
        if sid:
            self.sessions.append(
                sid, "card_surfaced",
                cmd_id=cmd.id, cmd_kind="preview_action", is_checkpoint=False,
                title=cmd.payload["title"], body_excerpt=cmd.payload["body"][:400],
                n_actions=len(subtasks),
            )
        await self._send_command(cmd)

    async def _dispatch_complex_agent(
        self,
        event: Event,
        *,
        add_dirs: list[str] | None = None,
        working_dir: str | None = None,
        timeout_s: float = 240.0,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        """The Phase 5 agent dispatch path — used by both /api/dev/agent_invoke
        and the classifier's complex branch in _handle_user_invoke.

        Builds the brief via cortex.agent_brief, picks Twin slices via the
        v0.5 selector, dispatches claude_code.agent, and surfaces the first
        card (checkpoint or final) via _send_agent_card_for_decision.
        """
        from .agent_brief import build_agent_brief, CANONICAL_ACTIONS_SCHEMA

        payload = event.payload or {}
        ask_text = (payload.get("text") or "").strip()

        # No Twin pre-load anymore (per Zack 2026-05-25). CC discovers
        # ~/constellation/twin/.claude/skills/ natively (Agent Skills format)
        # and reads identity.md / people/core/* on demand via its own Read
        # tool. The v0.5 selector LLM call was a brief-assembly step from
        # the round-based router era — it's pure latency now.
        twin_slices: dict[str, str] = {}

        schema_hint = output_schema or CANONICAL_ACTIONS_SCHEMA
        add_dirs = add_dirs or [
            os.path.expanduser("~/constellation/twin"),
            os.path.expanduser("~/Code/Projects"),
            os.path.expanduser("~/.claude/projects"),
        ]

        sid = (event.payload or {}).get("session_id")
        # P0.1 — reuse a still-alive CC tmux from a previous turn in the
        # same HUD session. agent_continue pastes the new ask into the
        # existing CC TUI (no fresh spawn, no brief re-load). CC retains
        # the system context (output schema, R1-R5 rules, twin skills,
        # available_dirs) from the original brief.
        reuse_entry = self._hud_tmux_lookup(sid) if sid else None
        if reuse_entry:
            await self._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="reusing_agent", icon="♻️",
                detail=(
                    f"reusing CC session {reuse_entry['cc_session_id'][:8]} "
                    f"(prior: {reuse_entry.get('last_summary') or 'idle'})"
                ),
            )
            if sid:
                self.sessions.append(
                    sid, "agent_dispatch",
                    event_id=event.id, reused=True,
                    cc_session_id=reuse_entry["cc_session_id"],
                    tmux_session=reuse_entry["tmux_session"],
                )
            try:
                rpc = await self._dispatch_to_tool({
                    "tool": "claude_code", "action": "agent_continue",
                    "args": {
                        "tmux_session": reuse_entry["tmux_session"],
                        "cc_session_id": reuse_entry["cc_session_id"],
                        "user_text": ask_text,
                        "working_dir": reuse_entry.get("working_dir") or working_dir,
                        "parent_event_id": event.id,
                        "timeout_s": timeout_s,
                        "keep_alive_on_final": True,
                    },
                    "result_format": "execute",
                })
            except Exception as e:
                log.warning("hud_tmux.reuse_failed", error=str(e), exc_info=True)
                await self._hud_tmux_evict(sid, reason="reuse_dispatch_failed")
                reuse_entry = None
            else:
                rpc_result = rpc.result or {}
                if not rpc_result.get("ok") or rpc_result.get("error"):
                    # Tmux gone, jsonl missing, or other reuse failure.
                    # Evict and fall through to fresh dispatch.
                    log.warning(
                        "hud_tmux.reuse_returned_error",
                        error=rpc_result.get("error"),
                    )
                    await self._hud_tmux_evict(sid, reason="reuse_returned_error")
                    reuse_entry = None
                else:
                    if sid:
                        self.sessions.append(
                            sid, "agent_completed",
                            event_id=event.id, reused=True,
                            cc_session_id=rpc_result.get("session_id"),
                            tmux_session=rpc_result.get("tmux_session"),
                            n_tool_uses=rpc_result.get("n_tool_uses"),
                            terminate_reason=rpc_result.get("terminate_reason"),
                            is_checkpoint=rpc_result.get("is_checkpoint"),
                        )
                    # Re-register (refresh last_activity + last_summary).
                    self._hud_tmux_register(
                        sid, rpc_result,
                        working_dir=reuse_entry.get("working_dir") or working_dir,
                        timeout_s=timeout_s,
                    )
                    await self._send_agent_card_for_decision(
                        rpc_result, event,
                        working_dir=reuse_entry.get("working_dir") or working_dir,
                        timeout_s=timeout_s,
                    )
                    return

        # UC1: if a photo rode in with this turn (e.g. a sendPhoto shortcut or a
        # "save this poster" memo), persist it under twin/ now and hand the agent
        # the relative path so it can embed the image in a memo. (On-demand
        # vision pulls happen later in the subtask loop and aren't for memos.)
        photo_path: str | None = None
        photo_summary: str | None = None
        if payload.get("image"):
            persisted = _persist_image_to_twin(
                payload["image"], event.id.replace("evt_", "")[:8])
            if persisted:
                photo_path = persisted["rel_to_memos"]
                log.info("photo.persisted_to_twin", path=persisted["abs"],
                         bytes=persisted["bytes"])
                await self._emit_progress_to_glass(
                    parent_event_id=event.id, stage="photo_saved", icon="🖼",
                    detail="photo saved to twin",
                )
                # UC1: analyse the photo NOW (CC can't see JPEGs) so the agent
                # can paste a ready 简介 into the memo. detail by intent (C-64);
                # the agent decides whether to use it.
                try:
                    await self._emit_progress_to_glass(
                        parent_event_id=event.id, stage="vision", icon="👁",
                        detail="analysing the photo…",
                    )
                    vrpc = await self._dispatch_to_tool({
                        "tool": "vision_describe", "action": "describe",
                        "args": {
                            "_image_b64": payload["image"],
                            "prompt": ask_text or "Describe this image.",
                            "detail": _vision_detail_for(ask_text),
                        },
                        "context_pack": [], "result_format": "execute",
                    })
                    photo_summary = (vrpc.result or {}).get("description") or None
                    log.info("photo.vision_summary",
                             chars=len(photo_summary or ""))
                except Exception as e:
                    log.warning("photo.vision_failed", error=str(e))

        brief = build_agent_brief(
            ask_text=ask_text,
            now_iso=datetime.now(timezone.utc).astimezone().isoformat(),
            has_photo=bool(payload.get("image")),
            photo_path=photo_path,
            photo_summary=photo_summary,
            twin_slices=twin_slices,
            output_schema=schema_hint,
            available_dirs=add_dirs,
        )
        await self._emit_progress_to_glass(
            parent_event_id=event.id,
            stage="brief_assembled", icon="📝",
            detail=f"agent brief assembled ({len(brief)} chars)",
        )

        # Per-conversation permission mode (decided from the creating utterance;
        # see _permission_mode_for). acceptEdits by default → CC prompts for
        # non-edit tools, surfaced to Zack as cards; bypass only on explicit
        # "auto-run" opt-in.
        permission_mode = _permission_mode_for(ask_text)
        log.info("agent.permission_mode", mode=permission_mode,
                 ask_preview=ask_text[:50])

        # Dispatch the agent action. RPC returns when CC end_turn's
        # (checkpoint or final).
        try:
            await self._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="dispatching_agent", icon="🤖",
                detail=f"dispatching agent ({permission_mode})",
            )
            if sid:
                self.sessions.append(
                    sid, "agent_dispatch",
                    event_id=event.id, brief_chars=len(brief),
                    add_dirs=add_dirs, timeout_s=timeout_s,
                )
            rpc = await self._dispatch_to_tool({
                "tool": "claude_code", "action": "agent",
                "args": {
                    "brief": brief,
                    "output_schema_hint": schema_hint,
                    "add_dirs": add_dirs,
                    "working_dir": working_dir,
                    "parent_event_id": event.id,
                    "timeout_s": timeout_s,
                    "permission_mode": permission_mode,
                    # P0.1: leave tmux alive so the next turn in this HUD
                    # session can paste into it via agent_continue.
                    "keep_alive_on_final": True,
                },
                "result_format": "execute",
            })
            # On RPC success, record the spawned CC session for the archive view.
            if sid and rpc.result:
                self.sessions.append(
                    sid, "agent_completed",
                    event_id=event.id,
                    cc_session_id=rpc.result.get("session_id"),
                    tmux_session=rpc.result.get("tmux_session"),
                    n_tool_uses=rpc.result.get("n_tool_uses"),
                    terminate_reason=rpc.result.get("terminate_reason"),
                    is_checkpoint=rpc.result.get("is_checkpoint"),
                )
                # Register for reuse on next turn.
                self._hud_tmux_register(
                    sid, rpc.result,
                    working_dir=working_dir, timeout_s=timeout_s,
                )
        except Exception as e:
            log.error("agent_dispatch.failed", error=str(e), exc_info=True)
            if self._glass_conn:
                err_cmd = Command(
                    id=ids.command_id(), ts=datetime.now(timezone.utc),
                    kind="hud_show",
                    payload={
                        "title": "Agent failed",
                        "body": f"Couldn't dispatch agent: {e}",
                        "icon": "✗", "options": [],
                    },
                    requires_confirm=False, ttl_ms=20_000,
                )
                await self._glass_conn.send(err_cmd.model_dump_json())
            return

        await self._send_agent_card_for_decision(
            rpc.result or {}, event,
            working_dir=working_dir, timeout_s=timeout_s,
        )

    async def _inject_feedback_into_agent(self, tmux_session: str, text: str) -> None:
        """Use the existing claude_code tmux machinery to paste user feedback
        into the active CC session. Dispatched via the tool-agent so the same
        socket / set-buffer / paste-buffer logic is shared."""
        # Reuse claude_code adapter via the tool RPC. send_keys with literal
        # text + trailing Enter mimics how the user typing in the TUI works.
        # (paste-buffer is more reliable for multi-line; but most feedbacks are
        # one line, so send_keys with explicit Enter is fine.)
        await self._dispatch_to_tool({
            "tool": "claude_code", "action": "send_keys",
            "args": {"session_id": tmux_session, "keys": text + "\n", "literal": True},
            "result_format": "execute",
        })

    async def _handle_tool_reverse_wake(self, event: Event) -> None:
        """A Tool Agent adapter pushed a wake event (e.g. claude_code permission prompt).

        Build a `tool_card` HUD Command directly from the payload and ship it to Glass.
        When Glass replies with a user_decision matching one of the option ids, we look up
        a response action and dispatch (e.g., send_keys 'y\\n' to the claude_code session).
        """
        payload = event.payload or {}
        wake_kind = payload.get("wake_kind", "permission_request")
        from_tool = payload.get("from_tool", "unknown")
        context_str = payload.get("context") or "(no context)"
        options = payload.get("options") or []
        session_id = payload.get("session_id")

        log.info(
            "reverse_wake.received",
            from_tool=from_tool,
            wake_kind=wake_kind,
            session_id=session_id,
            n_options=len(options),
        )

        # Build option label list + a map from chosen option id → follow-up dispatch.
        # CC v2.1.x permission UI is a 3-option arrow-key menu:
        #   1. Yes                                            → Enter
        #   2. Yes, and always allow access to <scope>        → Down, Enter
        #   3. No                                             → Down, Down, Enter
        # We use tmux named-key mode (literal=False) for these.
        # A permission_request becomes a CHECKPOINT card with the same 3-way
        # semantics Zack uses everywhere (approve / modify / reject), so the ring
        # drives it (TAP / LONG / DOUBLE). We translate those to CC's arrow-key
        # permission menu under the hood:
        #   approve → "Yes" (Enter)
        #   reject  → "No"  (Down, Down, Enter)
        #   modify  → reject the action, then inject Zack's spoken correction
        #             (handled in _handle_user_decision; needs his text first).
        # (CC v2.1.x menu: 1.Yes=Enter · 2.Yes,always=Down,Enter · 3.No=Down,Down,Enter)
        option_labels = [opt.get("label", opt.get("id", "?")) for opt in options]
        wake_response_map: dict[str, dict[str, Any]] = {}
        wake_permission: dict[str, Any] | None = None
        if from_tool == "claude_code" and wake_kind == "permission_request" and options:
            def _send(keys: list[str]) -> dict[str, Any]:
                return {
                    "tool": "claude_code", "action": "send_keys",
                    "args": {"session_id": session_id, "keys": keys, "literal": False},
                    "context_pack": [], "result_format": "execute",
                }
            allow_keys = ["Enter"]            # option 1 "Yes"
            deny_keys = ["Down", "Down", "Enter"]  # option 3 "No"
            wake_response_map = {"approve": _send(allow_keys), "reject": _send(deny_keys)}
            wake_permission = {"session_id": session_id, "deny": _send(deny_keys)}
            option_labels = ["approve", "modify", "reject"]

        # An AskUserQuestion becomes a QUESTION card (Piece 3): a single "answer"
        # action. CC's options are laid out in the body; Zack answers in natural
        # language (ring → mic) and we drive CC's AskUserQuestion to its free-text
        # "Other" slot and type his answer. (other_idx / nav key-count are a
        # best-effort default — calibrate against a real AskUserQuestion TUI in
        # the device session.)
        wake_question: dict[str, Any] | None = None
        if from_tool == "claude_code" and wake_kind == "question" and options:
            other_idx = next(
                (i for i, o in enumerate(options)
                 if str(o.get("id", "")).lower() == "other"
                 or str(o.get("label", "")).lower() == "other"),
                len(options) - 1,
            )
            wake_question = {"session_id": session_id, "other_idx": other_idx,
                             "n_options": len(options)}
            option_labels = ["answer"]

        # User-facing name: never surface the internal "claude_code" tool id on a
        # card — it's just "Claude" to the wearer (Zack 2026-05-30).
        who = "Claude" if from_tool == "claude_code" else from_tool
        title_map = {
            "permission_request": "Claude Needs You",
            "question": "Claude Needs You",
            "completion_notice": f"{who} done",
            "error": f"{who} error",
            "surprising_event": who,
        }
        icon_map = {
            "permission_request": "⚙",
            "question": "❓",
            "completion_notice": "✓",
            "error": "✗",
            "surprising_event": "✦",
        }

        cmd = Command(
            id=ids.command_id(),
            ts=datetime.now(timezone.utc),
            kind="hud_show" if not options else "preview_action",
            payload={
                "title": title_map.get(wake_kind, from_tool),
                "body": context_str[:600],
                "icon": icon_map.get(wake_kind, "✦"),
                "options": option_labels,
            },
            requires_confirm=bool(options),
            ttl_ms=120_000,  # longer TTL — user might be away from Glass
        )
        # Synthetic "plan" so the receipt-writer path still has structure
        synthetic_plan = {
            "primary_intent": f"reverse_wake_{wake_kind}",
            "subtasks": [],
            "hud_response": {
                "kind": cmd.kind, "title": cmd.payload["title"], "body_template": context_str[:600],
                "options": option_labels, "icon": icon_map.get(wake_kind, "✦"),
            },
            "reasoning": f"reverse-wake event from {from_tool} ({wake_kind})",
        }
        self._pending_previews[cmd.id] = {
            "event": event,
            "plan": synthetic_plan,
            "subtask_results": [],
            "wake_response_map": wake_response_map,
            "wake_session_id": session_id,
            "wake_permission": wake_permission,
            "wake_question": wake_question,
        }
        await self._send_command(cmd)
        log.info("command.sent", id=cmd.id, kind=cmd.kind, source="reverse_wake")

        # If there's no actionable choice (e.g. completion_notice), this is a one-shot
        # info card; write the receipt now.
        if not options:
            self._pending_previews.pop(cmd.id, None)
            self._write_receipt(synthetic_plan, [], event.id)

    async def _handle_user_invoke(self, event: Event) -> None:
        # Session linkage: every ask begins or extends a HUD session.
        # event.payload.session_id (if set) ties this ask to an existing
        # thread; otherwise we mint a new one. We stash it on the event for
        # downstream code (_dispatch_complex_agent, _build_command,
        # _handle_user_decision) to attribute records correctly.
        payload = event.payload or {}
        ask_text = (payload.get("text") or "").strip()
        existing_sid = (payload.get("session_id") or "").strip() or None

        # R-14.b / C-56: voice pin/unpin commands. Whole-utterance regex
        # ("pin this" / "unpin" / "钉住" / "保留" / etc.) — if the entire
        # prompt is a pin command, flip the flag + emit a one-line confirmation
        # card, short-circuit. This is per-session (operates on the user's
        # current HUD session); needs no classifier or router round-trip.
        if ask_text and (_looks_pin_intent(ask_text) or _looks_unpin_intent(ask_text)):
            await self._handle_pin_command(event, existing_sid, ask_text)
            return

        # Voice-driven shortcut-slot config. "set shortcut 2 to …" EDITS a
        # fixed slot (the app owns slot content) rather than running a task —
        # parse it + push a `shortcut_config` frame to Glass, short-circuit.
        from .shortcut_config import looks_shortcut_config
        if ask_text and looks_shortcut_config(ask_text):
            await self._handle_shortcut_config(event, ask_text)
            return

        # UC2 — session browser. Two hooks, pick-first so a pick utterance that
        # also mentions "session" doesn't fall into a fresh listing:
        #   (a) a pending listing for this HUD session + a pick ("continue #2 …")
        #       → resume that archived CC session with the remainder instruction.
        #   (b) "list my sessions in <project>" → list + show a numbered card.
        from .session_browser import (
            looks_session_browse, parse_pick, list_project_sessions,
            extract_project_query, match_pick_by_title,
        )
        pending_browse = self._pending_session_browse.get(existing_sid or "_no_sid")
        if ask_text and pending_browse and not looks_session_browse(ask_text):
            idx, instruction = parse_pick(ask_text, len(pending_browse["sessions"]))
            if idx is not None:
                await self._handle_session_browse_pick(
                    event, pending_browse, idx, instruction)
                return
            # No #N? Try pick-by-TOPIC ("the gesture one"). A confident match
            # ENTERS the session (no inline instruction → await the next
            # utterance, matching Zack's "到这个标题里面" then "我要跟它讲X").
            if not pending_browse.get("await_instruction"):
                tmatch = match_pick_by_title(ask_text, pending_browse["sessions"])
                if tmatch is not None:
                    await self._handle_session_browse_pick(
                        event, pending_browse, tmatch, "")
                    return
            # Pick-only step left a single session awaiting its instruction →
            # treat this whole (non-pick, non-browse) utterance as that instruction.
            if pending_browse.get("await_instruction"):
                await self._handle_session_browse_pick(
                    event, pending_browse, 1, ask_text)
                return
        if ask_text and looks_session_browse(ask_text):
            await self._handle_session_browse(event, ask_text, existing_sid)
            return

        # R-14 / C-56 — voice-addressable session router. Runs BEFORE start_turn
        # so the routed target dictates which session the turn extends. Short-
        # circuits when 0 or 1 sessions are active (caller behavior unchanged).
        # On `decision="continue"` → mutate existing_sid to the routed target;
        # downstream `_dispatch_complex_agent` then naturally uses agent_continue
        # via the existing `_hud_tmux_lookup` reuse path.
        #
        # R-14.c: when re-injected from a confirmation-card decision, the
        # event carries `_skip_session_router=True` so we don't loop on the
        # second pass.
        active_sessions = self._gather_active_sessions()
        skip_router = bool(payload.pop("_skip_session_router", False))
        if not skip_router and len(active_sessions) >= 2 and ask_text:
            from .session_router import route_session, HIGH_CONFIDENCE
            routed = await route_session(
                text=ask_text,
                active_sessions=active_sessions,
                current_session_id=existing_sid,
                has_image=bool(payload.get("image")),
            )
            MID_CONFIDENCE_FLOOR = 0.4
            decision = routed["decision"]
            confidence = routed["confidence"]
            target = routed["target_session_id"]

            # R-14.d: cross-session context bleed. If the router flagged other
            # sessions whose info the user wants to LEND to this turn, prepend
            # their last_summary (each cap'd) to the prompt text. Done BEFORE the
            # decision branches so it applies regardless of route — including the
            # mid-conf confirmation path: the event we stash for re-injection
            # then already carries the augmented text (fixes the C-58↔C-59 drop
            # where the mid-conf `return` skipped the bleed). Source sessions are
            # read-only (no agent_continue, no state change). Caps bound runaway
            # augmentation: ≤3 sources, ≤1500 combined chars.
            MAX_CONTEXT_SOURCES = 3
            MAX_CONTEXT_TOTAL_CHARS = 1500
            context_from = (routed.get("context_from") or [])[:MAX_CONTEXT_SOURCES]
            if context_from:
                context_blocks: list[str] = []
                used_chars = 0
                for src_sid in context_from:
                    src = self._active_hud_session_tmux.get(src_sid)
                    if not src:
                        continue
                    src_title = src.get("title") or src_sid
                    src_summary = (src.get("last_summary") or "").strip()
                    if not src_summary:
                        continue
                    remaining = MAX_CONTEXT_TOTAL_CHARS - used_chars
                    if remaining <= 0:
                        break
                    block_body = src_summary[: min(400, remaining)]
                    block = f"[context from session '{src_title}']\n{block_body}"
                    context_blocks.append(block)
                    used_chars += len(block)
                if context_blocks:
                    log.info(
                        "session_router.context_bled",
                        from_sids=context_from, n_blocks=len(context_blocks),
                    )
                    augmented = "\n\n".join(context_blocks) + "\n\n---\n\n" + ask_text
                    event.payload["text"] = augmented
                    ask_text = augmented
                    await self._emit_progress_to_glass(
                        parent_event_id=event.id,
                        stage="routing", icon="🔗",
                        detail=f"lent context from {len(context_blocks)} session(s)",
                    )

            if decision == "continue" and target and target != existing_sid \
                    and confidence >= HIGH_CONFIDENCE:
                # High-confidence continue → silently route
                title = next((s.get("title") for s in active_sessions
                              if s["session_id"] == target), None) or target
                log.info(
                    "session_router.switched",
                    from_sid=existing_sid, to_sid=target,
                    title=title, confidence=confidence,
                )
                await self._emit_progress_to_glass(
                    parent_event_id=event.id,
                    stage="routing", icon="🧭",
                    detail=f"routing to '{title}'",
                )
                existing_sid = target
            elif decision == "new" and confidence >= HIGH_CONFIDENCE:
                if existing_sid:
                    log.info(
                        "session_router.new",
                        from_sid=existing_sid, confidence=confidence,
                    )
                    await self._emit_progress_to_glass(
                        parent_event_id=event.id,
                        stage="routing", icon="🧭",
                        detail="starting a new session",
                    )
                existing_sid = None  # force start_turn to mint fresh
            elif decision == "continue" and target \
                    and MID_CONFIDENCE_FLOOR <= confidence < HIGH_CONFIDENCE:
                # R-14.c: medium confidence → ask the user via a confirmation
                # CARD (approve / modify / kill). The event is re-injected on
                # the user's decision (with _skip_session_router=True so we
                # don't loop).
                title = next((s.get("title") for s in active_sessions
                              if s["session_id"] == target), None) or target
                summary = next((s.get("last_summary") for s in active_sessions
                                if s["session_id"] == target), None) or ""
                log.info(
                    "session_router.confirm",
                    candidate=target, title=title,
                    confidence=confidence, why=routed.get("why"),
                )
                await self._emit_session_route_confirmation(
                    event=event,
                    candidate_session_id=target,
                    candidate_title=title,
                    candidate_summary=summary,
                    confidence=confidence,
                )
                return  # wait for user_decision before dispatching

        session_id_for_turn = self.sessions.start_turn(
            existing_session_id=existing_sid,
            event_id=event.id, ask_text=ask_text,
            has_image=bool(payload.get("image")),
        )
        # Attach to event payload for the rest of the pipeline.
        # Pydantic Event is frozen=False so we can mutate payload in place.
        event.payload["session_id"] = session_id_for_turn
        self._event_to_session[event.id] = session_id_for_turn

        # P0.3 — set the session-attribution ContextVar so every LLM call
        # fired inside this turn (classifier, router, agent_brief, etc.)
        # gets recorded against this session for cost roll-up.
        from .sessions import current_session_id as _csid
        _csid.set(session_id_for_turn)

        # R-13 / C-55: voice-with-vision detection. If the user's text looks
        # like a visual question but no image was attached (typical for a
        # voice-only invoke from glass), pull a scene capture from glass
        # NOW — before classifier + router. Reason: router refuses to pick
        # `vision_describe` when no image is in the event ("n_subtasks=0
        # primary_intent=unsupported"); classifier may correctly flag
        # "vision intent without photo" but then dispatches to CC which also
        # can't help. By prefetching, classifier sees has_image=True and
        # everything downstream just works.
        #
        # Heuristic is intentionally simple (regex) — no extra LLM call. False
        # positives waste a capture (~70KB, ~1.5s); false negatives just mean
        # the user gets a text-only answer. Both acceptable. Disabled when no
        # glass peer is connected (lookup needs the WSS) or when the event
        # already has an image (e.g. came from a photo shortcut).
        if (
            ask_text
            and not payload.get("image")
            and self._glass_conn
            and "card" in self._glass_accept
            and _looks_visual_intent(ask_text)
        ):
            log.info("vision.upfront_request_image", event_id=event.id, ask_preview=ask_text[:60])
            await self._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="capturing", icon="📷",
                detail="capturing scene…",
            )
            img = await self._request_image_from_glass(
                parent_event_id=event.id,
                hint="scene in front",
            )
            if img:
                event.payload["image"] = img
                log.info("vision.upfront_image_attached", event_id=event.id, bytes_b64=len(img))
            else:
                log.warning(
                    "vision.upfront_image_unavailable_fallback_textonly",
                    event_id=event.id,
                )

        # Phase 5c — classify intent first; complex asks bypass the v0.5
        # Router entirely and go straight to the CC agent path. Simple
        # asks (single-step state queries or explicit one-action requests)
        # continue through the existing planner + executor-adapter dispatch.
        if not self.use_stub_router:   # stub router (Phase 1) is for tests only
            from .classifier import classify_intent
            await self._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="classifying", icon="🧭",
                detail="classifying intent (gpt-5.2)",
            )
            try:
                decision = await classify_intent(event)
                why = str(decision.get("why") or "")[:80]
                self.sessions.append(
                    session_id_for_turn, "classifier",
                    event_id=event.id, complex=bool(decision.get("complex")), why=why,
                )
                if decision.get("complex"):
                    log.info("intent.complex_via_agent", why=decision.get("why"))
                    await self._emit_progress_to_glass(
                        parent_event_id=event.id,
                        stage="classified", icon="🧭",
                        detail=f"intent: complex → agent — {why}",
                    )
                    # R-13 / C-55: vision upfront-pull happened before
                    # classifier (see top of _handle_user_invoke). By the time
                    # we reach here, event.payload.image is set if the user's
                    # text looked visual; nothing to do at this layer.
                    await self._dispatch_complex_agent(event)
                    return
                log.info("intent.simple_via_router", why=decision.get("why"))
                await self._emit_progress_to_glass(
                    parent_event_id=event.id,
                    stage="classified", icon="🧭",
                    detail=f"intent: simple → planner — {why}",
                )
            except Exception as e:
                log.warning("classifier.errored_falling_through", error=str(e))
                # Fall through to existing path on classifier failure

        await self._emit_progress_to_glass(
            parent_event_id=event.id,
            stage="planning", icon="🧠",
            detail="planning dispatch (gpt-5.2 router)",
        )
        plan = await self._route(event)
        log.info("plan.generated", primary_intent=plan["primary_intent"])
        await self._emit_progress_to_glass(
            parent_event_id=event.id,
            stage="planned", icon="🎯",
            detail=f"plan: {plan['primary_intent']} · {len(plan.get('subtasks', []))} subtasks",
        )

        # Router occasionally slips kind="tool_card" for normal user_invoke; that
        # kind is reserved for reverse-wake (see _handle_tool_reverse_wake which
        # builds Commands directly). Normalize to preview_action so the SEND
        # gate still applies and confirm-policies fire below.
        hud = plan.get("hud_response", {})
        if hud.get("kind") == "tool_card":
            log.warning("router.tool_card_normalized_to_preview", primary_intent=plan["primary_intent"])
            hud["kind"] = "preview_action"

        plan = _apply_confirm_policies(plan, self._confirm_policies)

        # Any subtask blocked by `deny` policy aborts the plan with an error card
        denied = [st for st in plan["subtasks"] if st.get("_confirm_policy_denied")]
        if denied:
            log.warning("plan.denied_by_policy", denied=[f"{st['tool']}:{st['action']}" for st in denied])
            denied_card = Command(
                id=ids.command_id(),
                ts=datetime.now(timezone.utc),
                kind="hud_show",
                payload={
                    "title": "Action blocked",
                    "body": "One or more steps blocked by confirm-policies.md (`deny`).",
                    "icon": "✗",
                    "options": [],
                },
                requires_confirm=False,
                ttl_ms=15_000,
            )
            if self._glass_conn:
                await self._glass_conn.send(denied_card.model_dump_json())
            self._write_receipt(plan, [{}] * len(plan["subtasks"]), event.id)
            return

        hud_kind = plan["hud_response"]["kind"]

        # Subtask dispatch strategy depends on whether HUD requires user confirm:
        #   - preview_action: dispatch draft/query now (for preview); defer execute to SEND.
        #   - hud_show:       confirm-policy says auto. Dispatch ALL subtasks now so the
        #                     hud_show body can reflect real results; receipt written immediately.
        #
        # Q.4.5 / C-47: image bytes flow ONLY to tools the router explicitly
        # routed to that are declared vision-aware (see _VISION_AWARE_TOOLS).
        # Default = image goes nowhere; router has to opt in by routing to a
        # vision-capable tool based on the text prompt. The dispatcher itself
        # never decodes/inspects the image — it just hands the opaque bytes to
        # the selected vision tool's args.
        image_b64 = (event.payload or {}).get("image")
        # R-13 / C-55: if the router selected a vision-aware tool but the
        # originating event had no image attached, ask the glass peer to
        # capture one now (server-pull-on-demand). One round-trip per turn
        # regardless of how many vision-aware subtasks the plan has.
        if not image_b64 and any(
            st.get("tool") in _VISION_AWARE_TOOLS for st in plan["subtasks"]
        ):
            log.info(
                "vision.request_image",
                event_id=event.id,
                tools=[st.get("tool") for st in plan["subtasks"] if st.get("tool") in _VISION_AWARE_TOOLS],
            )
            await self._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="capturing", icon="📷",
                detail="capturing scene…",
            )
            image_b64 = await self._request_image_from_glass(
                parent_event_id=event.id,
                hint="scene in front",
            )
            if image_b64:
                log.info("vision.image_received", event_id=event.id, bytes_b64=len(image_b64))
            else:
                log.warning("vision.image_unavailable_fallback_to_textonly", event_id=event.id)
        subtask_results: list[dict[str, Any]] = []
        for i, st in enumerate(plan["subtasks"]):
            if st["result_format"] in ("draft", "query") or hud_kind == "hud_show":
                await self._emit_progress_to_glass(
                    parent_event_id=event.id,
                    stage="dispatch", icon="🔧",
                    detail=f"{st['tool']}.{st['action']} · subtask {i+1}/{len(plan['subtasks'])}",
                )
                # Re-interpolate args (subtask N may reference N-1's result)
                args = self._interpolate_args(st.get("args", {}), subtask_results, plan=plan)
                if image_b64 and st["tool"] in _VISION_AWARE_TOOLS:
                    args = {
                        **args,
                        "_image_b64": image_b64,
                        # Intent-driven cost control: high only when reading text.
                        "detail": args.get("detail") or _vision_detail_for(
                            (event.payload or {}).get("text") or ""
                        ),
                    }
                rpc_result = await self._dispatch_to_tool({**st, "args": args})
                subtask_results.append(rpc_result.result)
            else:
                subtask_results.append({})  # placeholder so indices align

        await self._emit_progress_to_glass(
            parent_event_id=event.id,
            stage="preparing_card", icon="🎴",
            detail=f"preparing {hud_kind}",
        )
        cmd = self._build_command(plan, subtask_results)
        sid = (event.payload or {}).get("session_id")
        self._pending_previews[cmd.id] = {
            "event": event,  # full event kept so we can re-route on Modify feedback
            "session_id": sid,
            "plan": plan,
            "subtask_results": subtask_results,
        }
        if sid:
            self.sessions.append(
                sid, "card_surfaced",
                cmd_id=cmd.id, cmd_kind=cmd.kind, is_checkpoint=False,
                title=cmd.payload.get("title", ""),
                body_excerpt=(cmd.payload.get("body") or "")[:400],
                primary_intent=plan.get("primary_intent"),
            )
        await self._send_command(cmd)
        log.info("command.sent", id=cmd.id, kind=cmd.kind)

        # hud_show = "done already, just informing"; no user gate, write receipt now.
        if hud_kind == "hud_show":
            self._pending_previews.pop(cmd.id, None)
            self._write_receipt(plan, subtask_results, event.id)

    async def _route(
        self,
        event: Event,
        feedback_iteration: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.use_stub_router:
            return route_stub(event)
        # ── Two-pass (v0.5): selector picks Twin paths → planner sees only those ──
        toc_entries = self.twin.build_toc()
        toc_paths = {p for p, _ in toc_entries}
        toc_table = self.twin.toc_as_table()
        picked = await select_twin_paths(
            event=event,
            twin_toc_table=toc_table,
            toc_paths=toc_paths,
            model=self.router_model,
        )
        context_pack = self.twin.assemble_context_pack(picked)
        return await route(
            event=event,
            available_tools_block=self._tools_block,
            allowed_tools=self._allowed_tools,
            model=self.router_model,
            context_pack=context_pack,
            feedback_iteration=feedback_iteration,
        )

    def _build_command(self, plan: dict[str, Any], results: list[dict[str, Any]]) -> Command:
        hud = plan["hud_response"]
        body = self._interpolate(hud["body_template"], results, plan=plan)
        # Three-option contract (Zack 2026-05-25 v2): every blocking card has
        # exactly Approve / Modify / Kill; info cards (hud_show) have no
        # buttons. Whatever the router emitted under hud_response.options is
        # ignored — cortex enforces the contract regardless of LLM drift.
        kind = hud["kind"]
        if kind == "preview_action":
            options: list[str] = list(_THREE_OPTIONS)
        else:
            options = []
        return Command(
            id=ids.command_id(),
            ts=datetime.now(timezone.utc),
            kind=kind,
            payload={
                "title": hud["title"],
                "body": body,
                "icon": hud.get("icon", ""),
                "options": options,
            },
            requires_confirm=kind == "preview_action",
            ttl_ms=30_000,
        )

    def _interpolate(self, template: str, results: list[dict[str, Any]], plan: dict[str, Any] | None = None) -> str:
        """Replace {{subtasks[i].result.field}} or {{subtasks[i].args.field}} with values.

        - `.result.field` reads from `results[i]` (filled at preview / SEND dispatch time).
        - `.args.field`   reads from `plan["subtasks"][i]["args"]` (statically known from plan).

        Minimal Jinja-ish: only the exact patterns used in CORTEX-ROUTER-PROMPT.md examples.
        """
        pattern = re.compile(
            r"\{\{\s*subtasks\[(\d+)\]\.(result|args)(?:\.([\w]+))?\s*\}\}"
        )

        def replace(m: re.Match) -> str:
            i = int(m.group(1))
            kind = m.group(2)  # "result" or "args"
            field = m.group(3)
            if kind == "result":
                if i >= len(results):
                    return m.group(0)
                r = results[i]
            else:  # "args"
                if plan is None or i >= len(plan.get("subtasks", [])):
                    return m.group(0)
                r = plan["subtasks"][i].get("args", {})
            if field is None:
                return json.dumps(r, ensure_ascii=False)
            return str(r.get(field, ""))

        return pattern.sub(replace, template)

    async def _handle_user_decision(self, event: Event) -> None:
        decision = event.payload.get("decision")
        cmd_id = event.payload.get("in_reply_to")
        feedback_text = event.payload.get("feedback_text")
        log.info("user_decision.received", decision=decision, cmd_id=cmd_id, has_feedback=bool(feedback_text))

        # R-14.c: drain session-route confirmation cards FIRST. The cmd_id
        # would match an entry we stashed in `_pending_session_routes` when
        # the router decided "continue" with medium confidence. On any of the
        # standard 3 outcomes (approve/modify/kill), we re-inject the original
        # user_invoke event with the chosen session_id so the rest of the
        # pipeline runs normally.
        session_route = self._pending_session_routes.pop(cmd_id, None)
        if session_route:
            kind, _resolved_text = _classify_user_decision(decision, feedback_text)
            log.info(
                "session_router.decision_received",
                cmd_id=cmd_id, kind=kind, candidate=session_route.get("candidate_session_id"),
            )
            await self._resolve_session_route_decision(session_route, kind)
            return

        # Peek (don't pop yet) — we may need to re-register if Modify lacks text.
        pending = self._pending_previews.get(cmd_id)
        if not pending:
            log.warning("user_decision.no_pending", cmd_id=cmd_id, known=list(self._pending_previews.keys()))
            return

        # Reverse-wake follow-up: option id matches the wake_response_map keys.
        # Keep the legacy literal path so allow_once / allow_always / deny still work.
        wake_response_map = pending.get("wake_response_map") or {}
        if wake_response_map and decision in wake_response_map:
            self._pending_previews.pop(cmd_id, None)
            try:
                follow_up = wake_response_map[decision]
                log.info("reverse_wake.responding", decision=decision, follow_up=follow_up)
                rpc_result = await self._dispatch_to_tool(follow_up)
                pending["subtask_results"] = [rpc_result.result]
                pending["plan"]["subtasks"] = [follow_up]
                self._write_receipt(pending["plan"], pending["subtask_results"], event.id)
            except Exception as e:
                log.error("reverse_wake_followup.failed", error=str(e), exc_info=True)
                raise
            return

        # Question card ANSWER (Piece 3): the wearer answers an AskUserQuestion
        # in natural language. First "answer" (no text) opens the mic; the
        # follow-up carries feedback_text → we drive CC's AskUserQuestion to its
        # free-text "Other" slot and type the answer. (nav key-count is a
        # best-effort default; calibrate against a real AskUserQuestion TUI.)
        wake_question = pending.get("wake_question")
        if wake_question and ((decision or "").strip().lower() == "answer" or feedback_text):
            answer_text = (feedback_text or "").strip()
            if not answer_text:
                log.info("reverse_wake.answer_needs_text", cmd_id=cmd_id)
                await self.emit_mic_open(stream_id=f"answer_{cmd_id}", ttl_ms=30_000)
                if self._glass_conn:
                    await self._glass_conn.send(json.dumps({
                        "id": f"prog_{ids.event_id()[4:]}", "kind": "progress",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "parent_event_id": pending["event"].id,
                        "stage": "answer_needs_text", "icon": "❓",
                        "detail": "Answer — say your reply.",
                    }, ensure_ascii=False))
                return  # leave the card pending for the spoken answer
            self._pending_previews.pop(cmd_id, None)
            try:
                sess = wake_question["session_id"]
                nav = ["Down"] * int(wake_question.get("other_idx", 0)) + ["Enter"]
                await self._dispatch_to_tool({
                    "tool": "claude_code", "action": "send_keys",
                    "args": {"session_id": sess, "keys": nav, "literal": False},
                    "context_pack": [], "result_format": "execute",
                })
                await asyncio.sleep(0.4)
                await self._inject_feedback_into_agent(sess, answer_text)
                log.info("reverse_wake.answer_injected", session=sess, text=answer_text[:60])
            except Exception as e:
                log.error("reverse_wake.answer_failed", error=str(e), exc_info=True)
            return

        # "dismiss" is a non-UI signal — fired by the web client on TTL
        # expiry or by programmatic callers. Drops the pending card and
        # kills any active agent tmux session. No button surfaces it.
        if (decision or "").strip().lower() == "dismiss":
            self._pending_previews.pop(cmd_id, None)
            log.info("dismissed", cmd_id=cmd_id)
            # Don't kill the tmux on dismiss — the card just timed out;
            # the user may still want to continue the session. Only the
            # explicit Kill button tears down the agent.
            return

        # Canonicalize: every blocking card has exactly three outcomes —
        # approve (proceed as previewed), modify (redirect with text), or
        # kill (abandon + clean up). Free-text classified by content.
        kind, resolved_text = _classify_user_decision(decision, feedback_text)
        log.info(
            "decision.classified",
            kind=kind,
            has_text=bool(resolved_text),
            from_button=bool(
                decision
                and decision.strip().lower()
                in (_APPROVE_BUTTON_TOKENS | _MODIFY_BUTTON_TOKENS | _KILL_BUTTON_TOKENS)
            ),
        )

        # Kill: terminate any active agent tmux, drop pending, log a kill
        # signal (negative training data for learning_queue), no further work.
        if kind == "kill":
            self._pending_previews.pop(cmd_id, None)
            sid = pending.get("session_id")
            if sid:
                self.sessions.append(
                    sid, "decision",
                    cmd_id=cmd_id, decision_kind="kill", text=None,
                )
                self.sessions.append(sid, "session_killed", cmd_id=cmd_id)
            _append_learning_signal(
                self.twin, event=pending["event"], pending=pending,
                decision_kind="kill", correction_text=None,
            )
            agent_result = pending.get("agent_result")
            tmux_session = (agent_result or {}).get("tmux_session")
            if tmux_session:
                try:
                    await self._dispatch_to_tool({
                        "tool": "claude_code", "action": "agent_kill",
                        "args": {"tmux_session": tmux_session},
                        "result_format": "execute",
                    })
                except Exception as e:
                    log.warning("agent_kill.failed", error=str(e))
            # P0.1 — drop registry entry so the next invoke in this HUD
            # session spawns fresh (Kill is an explicit reset signal).
            if sid:
                self._active_hud_session_tmux.pop(sid, None)
            if self._glass_conn:
                await self._glass_conn.send(json.dumps({
                    "id": f"prog_{ids.event_id()[4:]}",
                    "kind": "progress",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "parent_event_id": pending["event"].id,
                    "stage": "killed", "icon": "✗",
                    "detail": "killed at your request",
                }, ensure_ascii=False))
            log.info("decision.killed", cmd_id=cmd_id)
            return

        # Modify clicked but no text yet → re-surface the card, don't ack.
        # The web client is expected to focus the composer; user submits and
        # we get a follow-up user_decision with the text.
        if kind == "modify" and not resolved_text:
            log.info("decision.modify_needs_text", cmd_id=cmd_id)
            if self._glass_conn:
                await self._glass_conn.send(json.dumps({
                    "id": f"prog_{ids.event_id()[4:]}",
                    "kind": "progress",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "parent_event_id": pending["event"].id,
                    "stage": "modify_needs_text", "icon": "✍️",
                    "detail": "Modify — tell me how to change it.",
                }, ensure_ascii=False))
            # Mic opens HERE — only on explicit Modify (ring LONG_PRESS), not
            # speculatively on card entry. This is the single point where a
            # card-driven voice capture begins. Glass enforces its own 15s
            # hard cap (C-37) regardless of this ttl hint.
            await self.emit_mic_open(stream_id=f"modify_{cmd_id}", ttl_ms=30_000)
            return  # leave the card pending

        # ── Permission card MODIFY (Piece 2): reject the proposed action, then
        # inject Zack's spoken correction into the live CC so it re-plans.
        # (approve/reject already went through the wake_response_map path above.)
        wake_perm = pending.get("wake_permission")
        if kind == "modify" and resolved_text and wake_perm:
            self._pending_previews.pop(cmd_id, None)
            try:
                await self._dispatch_to_tool(wake_perm["deny"])  # press "No" on the prompt
                await asyncio.sleep(0.4)
                await self._inject_feedback_into_agent(
                    wake_perm["session_id"], resolved_text)
                log.info("reverse_wake.modify_injected",
                         session=wake_perm["session_id"], text=resolved_text[:60])
                if self._glass_conn:
                    await self._glass_conn.send(json.dumps({
                        "id": f"prog_{ids.event_id()[4:]}", "kind": "progress",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "parent_event_id": pending["event"].id,
                        "stage": "feedback_injected", "icon": "💬",
                        "detail": f"denied + redirected: \"{resolved_text[:50]}\"",
                    }, ensure_ascii=False))
            except Exception as e:
                log.error("reverse_wake.modify_failed", error=str(e), exc_info=True)
            return

        # ── Multi-phase agent checkpoint: resume CC with the canonical outcome ──
        if pending.get("is_checkpoint") and pending.get("agent_result"):
            self._pending_previews.pop(cmd_id, None)
            sid = pending.get("session_id")
            if sid:
                self.sessions.append(
                    sid, "decision",
                    cmd_id=cmd_id, decision_kind=kind, text=resolved_text,
                    at_checkpoint=True,
                )
            _append_learning_signal(
                self.twin, event=pending["event"], pending=pending,
                decision_kind=kind,
                correction_text=resolved_text,
            )
            if kind == "modify":
                self.distiller.on_modify(has_correction_text=bool(resolved_text))
            await self._resume_agent_phase(pending, decision, feedback_text, event)
            return

        # ── Modify-on-FINAL agent card: resume the same CC session via
        # --session-id (preserves CC's prior research context) instead of
        # falling through to v0.5 planner. Only fires on MODIFY; APPROVE
        # on FINAL still executes actions[] normally.
        if (
            kind == "modify"
            and resolved_text
            and pending.get("from_agent_final")
            and pending.get("agent_result")
        ):
            self._pending_previews.pop(cmd_id, None)
            sid = pending.get("session_id")
            if sid:
                self.sessions.append(
                    sid, "decision",
                    cmd_id=cmd_id, decision_kind=kind, text=resolved_text,
                    at_agent_final=True,
                )
            _append_learning_signal(
                self.twin, event=pending["event"], pending=pending,
                decision_kind=kind,
                correction_text=resolved_text,
            )
            self.distiller.on_modify(has_correction_text=bool(resolved_text))
            try:
                await self._resume_agent_with_modify(pending, resolved_text, event)
                return
            except ResumeFailed as e:
                # Genuine resume-failure (CC couldn't load the prior jsonl,
                # spawn failed, etc.). Fall through to the one-shot
                # _replan_with_feedback path — not as good (loses CC's
                # research context) but better than stranding the user.
                log.warning("modify_resume.failed_falling_back_to_replan", error=str(e))
                self._pending_previews[cmd_id] = pending
            except Exception as e:
                # Anything else (WSS dropped mid-card-send, transient IO,
                # etc.) is NOT a "resume failed" — the CC work happened OK,
                # only the delivery hiccuped. Don't double-do the work via
                # the legacy path; just log + return. The HUD will reconnect
                # and the user can re-issue if needed.
                log.warning("modify_resume.delivery_error_no_fallback", error=str(e))
                return

        # Commit: consume the pending entry now.
        self._pending_previews.pop(cmd_id, None)

        sid = pending.get("session_id")
        if sid:
            self.sessions.append(
                sid, "decision",
                cmd_id=cmd_id, decision_kind=kind, text=resolved_text,
                at_checkpoint=False,
            )

        # Implicit-learning signal: append this decision to the learning
        # queue. Both Approve (positive signal) and Modify (correction
        # signal) are valuable training data for Phase-7 skill distillation.
        _append_learning_signal(
            self.twin, event=pending["event"], pending=pending,
            decision_kind=kind,
            correction_text=resolved_text,
        )
        if kind == "modify":
            self.distiller.on_modify(has_correction_text=bool(resolved_text))

        plan = pending["plan"]
        original_event: Event = pending["event"]

        if kind == "approve":
            try:
                await self._execute_remaining(pending, event.id)
            except Exception as e:
                log.error("execute_remaining.failed", error=str(e), error_type=type(e).__name__, exc_info=True)
                raise
            return

        # kind == "modify" with text — one-shot re-plan with feedback.
        # (P1.1 ripout: no more task_history / recursive _advance_task.
        # Complex multi-step asks live in the agent path; the simple path
        # is strictly one round + optional feedback re-plan.)
        try:
            await self._replan_with_feedback(
                original_event, plan, resolved_text, src_evt=event.id,
            )
        except Exception as e:
            log.error("feedback_loop.failed", error=str(e), error_type=type(e).__name__, exc_info=True)
            raise

    async def _replan_with_feedback(
        self,
        original_event: Event,
        prior_plan: dict[str, Any],
        feedback_text: str,
        *,
        src_evt: str,
    ) -> None:
        """One-shot router re-plan with user feedback. Replaces the legacy
        recursive _advance_task — there's no multi-round loop anymore;
        complex multi-step research lives in the CC agent path.

        Used by:
        - Modify on a simple-path card (route through Router again with
          ZACK'S WORDS = feedback_text, get a revised plan, surface a new
          card the user can Approve/Modify/Kill).
        - ResumeFailed fallback when --resume of a prior CC session breaks:
          we have CC's prior structured output as 'prior_plan' and the
          user's modify text → ask Router to translate it into a simple
          fallback.
        """
        log.info("replan_with_feedback.start", text=feedback_text[:80])
        await self._emit_progress_to_glass(
            parent_event_id=original_event.id,
            stage="re_planning", icon="✍️",
            detail="re-planning with your correction",
        )
        feedback_iteration = {
            "feedback_text": feedback_text,
            "prior_plan_summary": {
                "primary_intent": prior_plan.get("primary_intent"),
                "reasoning": prior_plan.get("reasoning"),
                "hud_response": prior_plan.get("hud_response"),
            },
        }
        await self._emit_progress_to_glass(
            parent_event_id=original_event.id,
            stage="planning", icon="🧠",
            detail="re-running planner with your feedback",
        )
        next_plan = await self._route(
            original_event, feedback_iteration=feedback_iteration,
        )
        next_plan = _apply_confirm_policies(next_plan, self._confirm_policies)
        log.info(
            "replan_with_feedback.planned",
            primary_intent=next_plan["primary_intent"],
            n_subtasks=len(next_plan.get("subtasks", [])),
        )
        await self._emit_progress_to_glass(
            parent_event_id=original_event.id,
            stage="planned", icon="🎯",
            detail=f"plan: {next_plan['primary_intent']} · {len(next_plan.get('subtasks', []))} subtasks",
        )

        # Same dispatch flow as the first round of _handle_user_invoke.
        hud_kind = next_plan["hud_response"]["kind"]
        subtask_results: list[dict[str, Any]] = []
        for i, st in enumerate(next_plan["subtasks"]):
            if st["result_format"] == "query" or hud_kind == "hud_show":
                await self._emit_progress_to_glass(
                    parent_event_id=original_event.id,
                    stage="dispatch", icon="🔧",
                    detail=f"{st['tool']}.{st['action']} · subtask {i+1}/{len(next_plan['subtasks'])}",
                )
                args = self._interpolate_args(st.get("args", {}), subtask_results, plan=next_plan)
                rpc_result = await self._dispatch_to_tool({**st, "args": args})
                subtask_results.append(rpc_result.result)
            else:
                subtask_results.append({})

        await self._emit_progress_to_glass(
            parent_event_id=original_event.id,
            stage="preparing_card", icon="🎴",
            detail=f"preparing {hud_kind}",
        )
        cmd = self._build_command(next_plan, subtask_results)
        sid = (original_event.payload or {}).get("session_id")
        self._pending_previews[cmd.id] = {
            "event": original_event,
            "session_id": sid,
            "plan": next_plan,
            "subtask_results": subtask_results,
        }
        if sid:
            self.sessions.append(
                sid, "card_surfaced",
                cmd_id=cmd.id, cmd_kind=cmd.kind, is_checkpoint=False,
                title=cmd.payload.get("title", ""),
                body_excerpt=(cmd.payload.get("body") or "")[:400],
                primary_intent=next_plan.get("primary_intent"),
                from_replan=True,
            )
        await self._send_command(cmd)
        log.info("command.sent", id=cmd.id, kind=cmd.kind, from_replan=True)

        # hud_show = no user gate; write receipt now. (No auto-advance —
        # the re-plan is strictly one-shot.)
        if hud_kind == "hud_show":
            self._pending_previews.pop(cmd.id, None)
            self._write_receipt(next_plan, subtask_results, src_evt)

    async def _execute_remaining(self, pending: dict[str, Any], src_evt: str) -> None:
        """Run the `execute` subtasks after user SEND. Write receipt + CHANGELOG.

        After all subtasks complete, emit a terminal `completed` progress
        frame to Glass + log to the session. Without this the HUD's
        ActivityPill stays stuck on whatever the last pre-card progress
        was ("🎴 preparing preview_action"), giving the impression the
        action didn't happen even though it did.
        """
        plan = pending["plan"]
        results = list(pending["subtask_results"])
        parent_event_id = (pending.get("event").id if pending.get("event") else None) or src_evt
        sid = pending.get("session_id")
        for i, st in enumerate(plan["subtasks"]):
            if st["result_format"] == "execute":
                # Emit per-subtask progress so the HUD shows "executing X.Y"
                # instead of staying on the pre-card "preparing preview".
                await self._emit_progress_to_glass(
                    parent_event_id=parent_event_id,
                    stage="executing", icon="⚙️",
                    detail=f"{st['tool']}.{st['action']}",
                )
                # Re-interpolate args against accumulated results (subtask N may reference N-1)
                interpolated_args = self._interpolate_args(st.get("args", {}), results, plan=plan)
                st_to_dispatch = {**st, "args": interpolated_args}
                rpc_result = await self._dispatch_to_tool(st_to_dispatch)
                results[i] = rpc_result.result
                if sid:
                    self.sessions.append(
                        sid, "action_executed",
                        tool=st["tool"], action=st["action"],
                        result_brief=str(rpc_result.result)[:300] if rpc_result.result else None,
                    )

        self._write_receipt(plan, results, src_evt)
        # Terminal frame — ActivityPill hides on stage="completed", and the
        # chat gets a final "✓ done" row so the user sees execution succeeded.
        await self._emit_progress_to_glass(
            parent_event_id=parent_event_id,
            stage="completed", icon="✓",
            detail=f"done — {plan.get('primary_intent', 'action')}",
        )
        if sid:
            self.sessions.append(sid, "turn_complete", primary_intent=plan.get("primary_intent"))

    def _interpolate_args(self, args: Any, results: list[dict[str, Any]], plan: dict[str, Any] | None = None) -> Any:
        if isinstance(args, str):
            return self._interpolate(args, results, plan=plan)
        if isinstance(args, dict):
            return {k: self._interpolate_args(v, results, plan=plan) for k, v in args.items()}
        if isinstance(args, list):
            return [self._interpolate_args(v, results, plan=plan) for v in args]
        return args

    def _write_receipt(
        self, plan: dict[str, Any], results: list[dict[str, Any]], src_evt: str
    ) -> None:
        rcpt_id = ids.receipt_id()
        body = (
            f"\n## {datetime.now(timezone.utc).strftime('%H:%M:%S')} — "
            f"{plan['primary_intent']} [{rcpt_id}]\n"
            f"- evt: {src_evt}\n"
            f"- subtasks: {len(plan['subtasks'])}\n"
            f"- reasoning: {plan['reasoning']}\n"
        )
        for i, (st, r) in enumerate(zip(plan["subtasks"], results)):
            body += f"  - [{i}] {st['tool']}.{st['action']} ({st['result_format']}) → {json.dumps(r, ensure_ascii=False)[:160]}\n"
        self.twin.receipt_append(body)
        self.twin.changelog_append(
            summary=f"{plan['primary_intent']}",
            src=src_evt,
            details=[f"Appended receipt {rcpt_id}"],
        )
        log.info("executed", rcpt_id=rcpt_id)

    # ── Tool Agent connection + dispatch (with demux reader for reverse-wake) ──

    async def _ensure_tool_conn(self) -> None:
        """Open a persistent WSS to Tool Agent + spawn the demux reader.

        Idempotent. After this returns, self._tool_conn is connected and
        self._tool_reader_task is running, dispatching incoming messages to either
        per-rpc futures or the event bus.
        """
        if self._tool_conn is not None and self._tool_reader_task is not None and not self._tool_reader_task.done():
            return
        self._tool_conn = await websockets.connect(self.tool_agent_url)
        self._tool_reader_task = asyncio.create_task(self._tool_reader_loop())

    async def _tool_reader_loop(self) -> None:
        """Read from tool conn; demux RPCResult vs Event by message shape."""
        assert self._tool_conn is not None
        try:
            async for raw in self._tool_conn:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("tool.bad_json", raw_prefix=raw[:120] if isinstance(raw, str) else "")
                    continue
                # RPCResult has `id` matching a pending dispatch + `status`
                if "id" in msg and "status" in msg:
                    fut = self._pending_rpcs.pop(msg["id"], None)
                    if fut and not fut.done():
                        try:
                            fut.set_result(RPCResult(**msg))
                        except Exception as e:
                            fut.set_exception(e)
                    else:
                        log.warning("tool.orphaned_rpc_result", rpc_id=msg.get("id"))
                # Event (push from adapter watcher) has `kind`
                elif "kind" in msg:
                    log.info("tool.event_received", kind=msg.get("kind"))
                    try:
                        event = Event(**msg, id=ids.event_id())
                        await self._process_event(event)
                    except Exception as e:
                        log.error("tool.event_dispatch_failed", error=str(e), kind=msg.get("kind"), exc_info=True)
                else:
                    log.warning("tool.unknown_message_shape", keys=list(msg.keys()))
        except websockets.exceptions.ConnectionClosed:
            log.warning("tool.disconnected")
        finally:
            self._tool_conn = None
            self._tool_reader_task = None
            # Fail all pending dispatches
            for rpc_id, fut in list(self._pending_rpcs.items()):
                if not fut.done():
                    fut.set_exception(RuntimeError("Tool Agent connection closed"))
            self._pending_rpcs.clear()

    async def _dispatch_to_tool(self, subtask: dict[str, Any]) -> RPCResult:
        await self._ensure_tool_conn()
        dispatch = RPCDispatch(
            id=ids.rpc_id(),
            ts=datetime.now(timezone.utc),
            tool=subtask["tool"],
            action=subtask["action"],
            args=subtask.get("args", {}),
            context_pack=subtask.get("context_pack", []),
            result_format=subtask["result_format"],
        )
        if self.plane:
            self.plane.record_dispatch_start(
                rpc_id=dispatch.id, tool=dispatch.tool, action=dispatch.action,
                args=dispatch.args, result_format=dispatch.result_format,
            )
        # Per-dispatch RPC timeout. Default 120s for fast adapter actions; for
        # claude_code.agent / agent_continue (long-running CC sessions) read
        # args.timeout_s and add 30s slack so the RPC outlasts the action's own
        # internal cap. Otherwise the RPC times out mid-flight, cortex
        # disconnects, and the still-running tmux session is orphaned.
        rpc_timeout_s = 120.0
        if dispatch.tool == "claude_code" and dispatch.action in ("agent", "agent_continue"):
            inner_timeout = float(dispatch.args.get("timeout_s") or 300.0)
            rpc_timeout_s = inner_timeout + 30.0
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_rpcs[dispatch.id] = fut
        import time as _time
        t0 = _time.monotonic()
        try:
            await self._tool_conn.send(dispatch.model_dump_json())
            result = await asyncio.wait_for(fut, timeout=rpc_timeout_s)
            if self.plane:
                self.plane.record_dispatch_end(
                    rpc_id=dispatch.id,
                    status=result.status,  # "success" | "failure" | "needs_confirm" | "tool_paused"
                    result=result.result,
                    latency_ms=int((_time.monotonic() - t0) * 1000),
                )
            return result
        except asyncio.TimeoutError:
            self._pending_rpcs.pop(dispatch.id, None)
            if self.plane:
                self.plane.record_dispatch_end(
                    rpc_id=dispatch.id, status="error",
                    result={"error": f"timeout after {rpc_timeout_s:.0f}s"},
                    latency_ms=int((_time.monotonic() - t0) * 1000),
                )
            raise RuntimeError(f"RPC {dispatch.id} timed out after {rpc_timeout_s:.0f} s")


async def serve(
    host: str,
    port: int,
    twin: Twin,
    tool_agent_url: str,
    router_model: str = "gpt-5.2",
    use_stub_router: bool = True,
    plane: ControlPlane | None = None,
) -> None:
    server = CortexServer(
        twin=twin,
        tool_agent_url=tool_agent_url,
        router_model=router_model,
        use_stub_router=use_stub_router,
        plane=plane,
    )
    if plane is not None:
        plane.bind(server=server, twin=twin)
    # P1.4 — wire the insight engine. Default OFF; opt in via env var
    # CONSTELLATION_INSIGHT_ENGINE=1. No-op when disabled.
    from .insight_engine import InsightEngine, register_default_providers
    server.insight_engine = InsightEngine(server)
    register_default_providers(server.insight_engine)
    server.insight_engine.start()
    # P0.1 patch — background TTL sweeper for the per-HUD-session tmux registry.
    # Lazy eviction (in _hud_tmux_lookup) misses sessions whose owner never
    # comes back — this loop is what guarantees the upper bound on process count.
    server._hud_tmux_sweeper_task = asyncio.create_task(server._hud_tmux_sweeper_loop())
    # P2.3 — TCC self-check. Runs in background; if any Apple app is denied,
    # stash a hud_show that fires on the next Glass connect.
    from .tcc_check import run_and_surface as _tcc_run_and_surface
    asyncio.create_task(_tcc_run_and_surface(server))
    log.info("cortex.listening", host=host, port=port)
    async with websockets.serve(server.handle_glass, host, port):
        await asyncio.Future()  # run forever
