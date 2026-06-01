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
from .prompts import (
    VISION_KEYWORD_PATTERN as _VISION_KEYWORD_PATTERN,
    VISION_DETAIL_PATTERN as _VISION_DETAIL_PATTERN,
    GPT_OVERRIDE_PATTERN as _GPT_OVERRIDE_PATTERN,
    CLAUDE_OVERRIDE_PATTERN as _CLAUDE_OVERRIDE_PATTERN,
    AUTO_RUN_PATTERNS as _AUTO_RUN_PATTERNS,
    PIN_INTENT_PATTERNS as _PIN_INTENT_PATTERNS,
    UNPIN_INTENT_PATTERNS as _UNPIN_INTENT_PATTERNS,
    THREE_OPTIONS as _THREE_OPTIONS,
    APPROVE_BUTTON_TOKENS as _APPROVE_BUTTON_TOKENS,
    MODIFY_BUTTON_TOKENS as _MODIFY_BUTTON_TOKENS,
    KILL_BUTTON_TOKENS as _KILL_BUTTON_TOKENS,
)
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
# Kill    = abandon: kill any agent + drop pending + log a kill signal.
# _THREE_OPTIONS and the vision cue _VISION_KEYWORD_PATTERN now live in
# prompts.py (imported at the top). Vision is a deterministic keyword opt-in:
# saying 「视觉」 captures a frame and hands it, unchanged, to whichever path runs.


def _looks_visual_intent(text: str) -> bool:
    """True iff the text carries the vision cue 「视觉」 (+ near-mishears / pinyin /
    "vision", see _VISION_KEYWORD_PATTERN) — the deterministic opt-in Zack
    controls. No LLM, no broad heuristic: 「视觉」 is the ONLY capture trigger.
    「细节」 alone never fires (it's a common word); it only UPGRADES the tier when
    paired as 「细节视觉」 (see _vision_tier_for). On a hit Cortex captures a frame and
    hands it (unchanged) downstream; the model reads it at full fidelity."""
    return bool(text and _VISION_KEYWORD_PATTERN.search(text))


def _vision_tier_for(text: str) -> str:
    """Pick the CAPTURE tier once the 「视觉」 cue fired (Zack 2026-06-01): a 「细节」
    prefix → 「细节视觉」 (+ 高清 / 2k / mishears, see _VISION_DETAIL_PATTERN) →
    'detail' (high-res 2K, for reading fine text — poster/sign/document); the bare
    「视觉」 glance → 'standard' (1080p scene). Deterministic regex, no LLM. Only the
    tier NAME goes to the glasses (in request_image); the glasses map name → px."""
    return "detail" if (text and _VISION_DETAIL_PATTERN.search(text)) else "standard"


def _model_override_for(text: str) -> str | None:
    """Deterministic model pin (Zack 2026-06-01): naming a model in the ask forces
    its path, no LLM guess (mirrors the vision cue). 'claude'/'克劳德'/'cloud' →
    the complex agent path; 'gpt'/'chatgpt'/'openai' → the simple router path.
    Claude wins ties — the agent path can degrade to a single action, but the
    router can't escalate to research. Returns 'claude' | 'gpt' | None."""
    if not text:
        return None
    if _CLAUDE_OVERRIDE_PATTERN.search(text):
        return "claude"
    if _GPT_OVERRIDE_PATTERN.search(text):
        return "gpt"
    return None


# Per-conversation permission mode (Zack 2026-05-30). The agent's CC permission
# mode is decided from the CREATING utterance and sticks for the conversation:
#   - explicit "auto-run everything / I approve, just go" → bypassPermissions
#     (CC runs every tool with no prompt).
#   - default, or "I need to verify this" → acceptEdits ("edit mode"): CC
#     auto-accepts file edits but PROMPTS for Bash/exec/other — and those prompts
#     surface to Zack as checkpoint cards (Piece 2). Answer-needs (AskUserQuestion)
#     surface as question cards (Piece 3).
# Full-auto (bypassPermissions) is opt-in ONLY via the explicit phrase "自动模式"
# (Zack 2026-05-30: "有'自动模式'这四个字的时候，才能开"). No other phrasing enables it —
# everything else stays acceptEdits, so permission requests surface as cards.
# _AUTO_RUN_PATTERNS now lives in prompts.py (imported at the top).


def _permission_mode_for(text: str) -> str:
    """Decide the CC permission mode for a fresh conversation from its first
    utterance (Zack 2026-05-30):
      - 'bypassPermissions' (full auto, no permission cards) ONLY when the
        utterance literally contains "自动模式" — the sole opt-in.
      - otherwise 'acceptEdits' (the DEFAULT): file edits auto-apply, but every
        other tool surfaces a checkpoint card and AskUserQuestion a question card."""
    if text:
        for p in _AUTO_RUN_PATTERNS:
            if p.search(text):
                return "bypassPermissions"
    return "acceptEdits"


def _use_sdk_agent() -> bool:
    """Complex tasks always run on the in-process Claude Agent SDK single source
    (claude_sdk_agent). The tmux dual-worker is RETIRED (2026-05-30, Rev 18 C-72)
    — there is no fallback. Kept as a function so the call sites read clearly;
    the env var is no longer consulted."""
    return True


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


_TWIN_CAPTURES = os.path.expanduser("~/constellation/twin/captures")


def _archive_capture(raw: bytes, req_id: str | None) -> str | None:
    """Save EVERY glasses photo that reaches Cortex, used or not (Zack 2026-06-01):
    the bytes already crossed the wire, so keep a copy — for later memo use,
    debugging, or simply not losing a capture. Written to
    twin/captures/<utc-timestamp>-<req_id>.jpg (req_id is unique per request, so no
    same-second collisions). Best-effort: never raises into the receive loop.
    Returns the absolute path or None. (Distinct from _persist_image_to_twin, which
    only fires when an image is being EMBEDDED in a memo.)"""
    if not raw:
        return None
    try:
        os.makedirs(_TWIN_CAPTURES, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        fname = f"{ts}-{(req_id or 'noid')[:12]}.jpg"
        abs_path = os.path.join(_TWIN_CAPTURES, fname)
        with open(abs_path, "wb") as f:
            f.write(raw)
        return abs_path
    except OSError:
        return None


# R-14.b / C-56: pin/unpin intent detection. Tiny regex on the user's
# voice-invoke text — when the entire utterance is a pin command, we
# short-circuit the normal classifier+dispatch flow, flip the session's
# `pinned` flag, emit a confirmation card, and stop. Pin patterns cover EN
# + ZH; the test is "is the ENTIRE prompt a pin instruction?" — not "does
# it contain pin words" (which would false-positive on "pin a reminder
# about X"). _PIN_INTENT_PATTERNS / _UNPIN_INTENT_PATTERNS live in prompts.py
# (imported at the top).


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


# _APPROVE/_MODIFY/_KILL_BUTTON_TOKENS now live in prompts.py (imported at top).
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


def _classify_user_decision(
    decision: str | None,
    feedback_text: str | None,
) -> tuple[str, str | None]:
    """Return ('approve' | 'modify' | 'kill', resolved_text_for_modify_or_None).

    The ring maps every card gesture to a fixed canonical token before it reaches
    us — TAP→"Approve", LONG→"modify", double-tap→"Kill" (see Glass StateMachine)
    — so `decision` is always one of the three labels; a modify carries its
    correction in `feedback_text`. The old free-text reply path (synonym lists +
    spoken-content sniffing) died with the ring-only model (Zack 2026-06-01): no
    spoken phrase ever becomes the `decision` field.
    """
    d = (decision or "").strip().lower()
    if d in _APPROVE_BUTTON_TOKENS:
        return "approve", None
    if d in _KILL_BUTTON_TOKENS:
        return "kill", None
    if d in _MODIFY_BUTTON_TOKENS:
        return "modify", (feedback_text or "").strip() or None
    # Unknown token (the ring only ever emits the three above): never auto-approve
    # or auto-kill on an unrecognized signal — treat as modify so the caller
    # re-surfaces the card (the safe, non-destructive default).
    return "modify", (feedback_text or "").strip() or None


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
        # STT-review gate (Zack 2026-05-30): EVERY voice transcript surfaces an
        # "STT review" card and waits for the wearer's approval BEFORE any
        # GPT/router/Claude-Code/send. Keyed by the review card's cmd_id →
        # {transcript, intent, orig_cmd_id, session_id, lang_hint}. Iron rule:
        # raw STT never reaches a downstream without an explicit approve.
        self._pending_stt_review: dict[str, dict[str, Any]] = {}
        # P1 — outstanding Agent-SDK permission/question cards keyed by cmd_id;
        # each holds the can_use_tool future that _handle_user_decision resolves
        # (see claude_sdk_agent). Empty unless USE_SDK_AGENT is on.
        self._sdk_pending: dict[str, dict[str, Any]] = {}
        # P2 — in-flight SDK agent runs keyed by event.id, so a kill decision
        # can interrupt the running turn (SdkAgentSession.interrupt()).
        self._sdk_active: dict[str, Any] = {}
        # R-13 / C-55: server-pull-on-demand vision. When router selects a
        # vision-aware tool but `event.payload.image` is None, Cortex emits
        # `request_image` to glass and awaits a matching `image_attached`
        # event. Future keyed by req_id (server-minted); resolved with the
        # image b64 string (or None on timeout/empty payload).
        self._pending_image_requests: dict[str, asyncio.Future[str | None]] = {}
        # When an `image_attached` header frame announces a binary upload
        # (binary:true), we stash (req_id, mime) here so the NEXT binary WS
        # frame can be paired with it. Single-glass + serialized capture means
        # at most one is ever in flight.
        self._pending_binary_image: tuple[str, str] | None = None

        # ── Unified glass OUTBOX (Zack 2026-05-30) ─────────────────────────
        # ALL outbound glass frames — agent progress, cards, insights, mic, … —
        # go through ONE ordered queue drained by a SINGLE sender coroutine.
        # Handlers enqueue synchronously (put_nowait, never suspend), so enqueue
        # order == event-processing order == send order. Kills the prior tech
        # debt where many async paths each `await glass_conn.send(...)`
        # independently could interleave / arrive out of order (a slow worker's
        # earlier event landing AFTER a faster later one). Per-connection:
        # created on connect, cancelled + dropped on disconnect.
        # Outbox items are (payload, blocks_until_decision). When a decision card
        # (checkpoint/question/stt_review/permission) is sent, the sender PAUSES
        # after it until the wearer's decision arrives — later frames (progress)
        # queue up behind it and are NEVER dropped or allowed to bury the card.
        # `_decision_gate` is the pause latch: set = flow open, cleared = waiting.
        self._glass_outbox: asyncio.Queue[tuple[str, bool]] | None = None
        self._glass_sender_task: asyncio.Task[None] | None = None
        self._decision_gate: asyncio.Event | None = None
        # Background tasks spawned off the glass receive loop (agent runs etc.)
        # so the loop never blocks and can always deliver a mid-run decision.
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._glass_seq = 0

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
                # vision_describe REMOVED (2026-05-31): no image→text tool. A photo
                # rides into the planner (route()) as a multimodal image block.
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

    def _spawn_bg(self, coro: Any, label: str) -> "asyncio.Task[Any]":
        """Run a coroutine off the current loop, keeping a strong ref (so it's
        not GC'd mid-flight) and logging any exception (create_task swallows
        them otherwise). Used to keep the glass receive loop non-blocking."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)

        def _done(t: "asyncio.Task[Any]") -> None:
            self._bg_tasks.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error("bg_task.failed", label=label, error=str(e), exc_info=True)

        task.add_done_callback(_done)
        return task

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
        # Spin up the single ordered sender for THIS connection — the only
        # coroutine that writes the glass socket (see _glass_send).
        self._glass_outbox = asyncio.Queue()
        self._decision_gate = asyncio.Event()
        self._decision_gate.set()  # open; cleared only while a decision card waits
        self._glass_seq = 0
        self._glass_sender_task = asyncio.create_task(self._glass_sender_loop(ws))
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
                self._glass_send(cmd.model_dump_json())
                log.info("startup_card.delivered", title=pending_startup["title"][:60])
            except Exception as e:
                log.warning("startup_card.send_failed", error=str(e))
            self._pending_startup_card = None
        try:
            async for raw in ws:
                # Binary frames carry raw image bytes (the glass→cortex photo
                # upload), paired with a preceding `image_attached` header frame.
                # Everything else is a JSON text event.
                if isinstance(raw, (bytes, bytearray)):
                    await self._handle_binary_image_frame(bytes(raw))
                    continue
                event_data = json.loads(raw)
                # Liveness preflight (glass → cortex → glass): the glass pings
                # before a wake to confirm the WSS path is live (BT-PAN idle-drops
                # silently). Reply immediately — bypasses the accept filter and
                # isn't gated, so it flows whenever no decision is parked (true on
                # a wake from Idle). Skip Event construction + log spam.
                if event_data.get("kind") == "ping":
                    self._glass_send(json.dumps(
                        {"kind": "pong", "ts": datetime.now(timezone.utc).isoformat()}))
                    continue
                event_data.pop("id", None)  # Cortex assigns ids on ingress
                event = Event(**event_data, id=ids.event_id())
                log.info("glass.event", id=event.id, kind=event.kind)
                # Audio frames must stay ordered + are fast → inline. Everything
                # else (decisions, the synthesized invoke after STT-approve) may
                # kick off a LONG agent run that itself awaits a FURTHER glass
                # decision (permission / answer / modify). Running that inline
                # blocks this receive loop, so that decision can never arrive →
                # the agent waits forever for an approval it can't get (deadlock
                # observed 2026-05-30 on the permission flow). Spawn it off-loop.
                if event.kind in ("audio_chunk", "audio_end"):
                    await self._process_event(event)
                else:
                    self._spawn_bg(self._process_event(event), f"glass:{event.kind}")
        except websockets.exceptions.ConnectionClosed:
            log.info("glass.disconnected")
        finally:
            self._glass_conn = None
            # Tear down this connection's ordered sender + drop any queued frames
            # (they belong to the peer that just left; the next peer starts clean).
            if self._glass_sender_task is not None:
                self._glass_sender_task.cancel()
                self._glass_sender_task = None
            self._glass_outbox = None

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

        # NOTE: the old "drop progress while a decision is pending" hack was
        # removed (Zack 2026-05-30). The unified outbox now PARKS the sender
        # after a decision card instead of discarding progress — nothing is
        # dropped; queued progress simply waits behind the card and flows once
        # the decision is made. No舍弃.

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
            self._glass_send(json.dumps(frame, ensure_ascii=False))
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
        meta: str | None = None,
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
            self._glass_send(json.dumps(frame, ensure_ascii=False))
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
                # Persistent model tag (Zack 2026-05-31): the wearer should always
                # know whether GPT (classify/route) or Claude (agent) is working.
                meta_runs=([{"text": meta, "style": "dim"}] if meta else []),
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
                self._glass_send(json.dumps({
                    "id": f"prog_{ids.event_id()[4:]}",
                    "kind": "progress",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "parent_event_id": parent_event_id,
                    "stage": "feedback_noted",
                    "icon": "👂",
                    "detail": f"heard \"{text[:40]}\" — continuing",
                }, ensure_ascii=False))
            return

        # Substantive feedback during a progress window used to be pasted into
        # the live CC tmux session via send-keys (the retired tmux dual-worker).
        # That path is gone: the in-process SDK agent owns its own turn and never
        # registers a tmux_session in _active_agents, so this branch is dead. The
        # SDK surfaces mid-run input through its native permission/question cards
        # (see claude_sdk_agent), not this progress-feedback channel.
        log.info("progress_feedback.noop_no_tmux", parent=parent_event_id, text=text[:80])

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
                self._glass_send(json.dumps({
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
            # STILL surface an stt_review card — Glass already swapped to its
            # loading placeholder on utterance-end (#3); without a card it would
            # spin forever. Empty transcript → guide a re-speak (Zack 2026-05-30).
            log.info("audio_end.empty_transcript")
            review_cmd_id = ids.command_id()
            self._pending_stt_review[review_cmd_id] = {
                "transcript": "", "intent": intent, "orig_cmd_id": cmd_id,
                "session_id": session_id, "lang_hint": lang_hint,
            }
            await self.emit_card(
                cmd_id=review_cmd_id, title="STT review",
                body_md="没听清 —— 长按重讲", options=["approve", "modify"],
                card_type="stt_review", source="Whisper", ttl_ms=120_000,
            )
            return

        # ── STT-REVIEW GATE (Zack 2026-05-30) ────────────────────────────────
        # IRON RULE: raw STT never reaches a downstream (GPT classifier/router,
        # Claude Code answer/modify, or any send) without the wearer's explicit
        # approval. Whatever the intent (fresh / modify / answer), we FIRST
        # surface an "STT review" card showing the transcript and stop here. On
        # approve → `_route_stt_approved` runs the original routing; on modify →
        # `_respeak_stt` re-opens the right mic. This is the ONLY exit from
        # audio_end now — no intent path bypasses the review.
        review_cmd_id = ids.command_id()
        self._pending_stt_review[review_cmd_id] = {
            "transcript": transcript,
            "intent": intent,
            "orig_cmd_id": cmd_id,
            "session_id": session_id,
            "lang_hint": lang_hint,
        }
        await self.emit_card(
            cmd_id=review_cmd_id,
            title="STT review",
            body_md=transcript,
            options=["approve", "modify"],
            card_type="stt_review",
            source="Whisper",
            ttl_ms=120_000,
        )
        log.info("stt_review.surfaced", review_cmd_id=review_cmd_id,
                 intent=intent, orig_cmd_id=cmd_id, n_chars=len(transcript))

    async def _route_stt_approved(self, stt: dict[str, Any]) -> None:
        """STT review APPROVED → now route the reviewed transcript per its
        original intent. This is the single point where voice enters the real
        pipeline (GPT / Claude Code). Mirrors the pre-gate audio_end routing."""
        transcript = stt["transcript"]
        intent = stt.get("intent", "fresh")
        orig = stt.get("orig_cmd_id")
        session_id = stt.get("session_id")
        if not transcript.strip():
            # Empty-transcript card was approved instead of redone — nothing to
            # route. No-op (the wearer can re-speak from Idle). Never sends empty
            # text downstream (Zack 2026-05-30).
            log.info("stt_review.approved_empty_noop")
            return
        if intent == "modify" and orig:
            synth = Event(
                id=ids.event_id(), kind="user_decision",
                ts=datetime.now(timezone.utc),
                payload={"in_reply_to": orig, "decision": "Modify",
                         "feedback_text": transcript},
            )
            await self._handle_user_decision(synth)
        elif intent == "answer" and orig:
            synth = Event(
                id=ids.event_id(), kind="user_decision",
                ts=datetime.now(timezone.utc),
                payload={"in_reply_to": orig, "decision": "answer",
                         "feedback_text": transcript},
            )
            await self._handle_user_decision(synth)
        else:
            synth = Event(
                id=ids.event_id(), kind="user_invoke",
                ts=datetime.now(timezone.utc),
                payload={"text": transcript, "session_id": session_id},
            )
            await self._handle_user_invoke(synth)

    async def _respeak_stt(self, stt: dict[str, Any]) -> None:
        """STT review → MODIFY (重讲): re-open the right mic to say it again,
        preserving the original intent so the re-spoken transcript routes the same
        way once re-approved. (The 'redo' token was purged 2026-06-01 — LONG is
        uniformly 'modify' / give-input-again across every card.)"""
        intent = stt.get("intent", "fresh")
        orig = stt.get("orig_cmd_id")
        if intent == "modify" and orig:
            await self.emit_mic_open(stream_id=f"modify_{orig}", ttl_ms=30_000)
        elif intent == "answer" and orig:
            await self.emit_mic_open(stream_id=f"answer_{orig}", ttl_ms=30_000)
        else:
            await self.emit_mic_open(stream_id=f"fresh_{ids.event_id()[4:]}", ttl_ms=30_000)

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

    def _glass_send(self, payload: str, *, blocks: bool = False) -> None:
        """Enqueue ONE serialized frame onto the unified glass outbox. Synchronous
        and non-suspending (put_nowait) so enqueue order is preserved EXACTLY as
        the send order by the single _glass_sender_loop — no interleaving, no
        out-of-order arrival across async producers. `blocks=True` marks a
        decision card: after sending it the sender pauses until the wearer
        decides (later frames wait in the queue, never dropped). No-op if no
        glass peer is connected (outbox is None)."""
        ob = self._glass_outbox
        if ob is None:
            return
        self._glass_seq += 1
        ob.put_nowait((payload, blocks))

    async def _glass_sender_loop(self, ws: Any) -> None:
        """The SOLE writer to the glass socket. Drains the outbox in FIFO order
        so frames reach the eyewear exactly in enqueue order. After a decision
        card (blocks=True) it PARKS on _decision_gate until the wearer's decision
        clears it — so a permission/checkpoint card is never buried by the
        progress that the jsonl tail emits right after it (replaces the old
        'drop the progress' hack; nothing is discarded, it just waits)."""
        ob = self._glass_outbox
        gate = self._decision_gate
        if ob is None or gate is None:
            return
        try:
            while True:
                payload, blocks = await ob.get()
                try:
                    await ws.send(payload)
                except Exception as e:
                    log.warning("glass_send.failed", error=str(e))
                if blocks:
                    # Park until the decision arrives (gate set by
                    # _handle_user_decision). Queued progress waits behind us.
                    gate.clear()
                    log.info("glass_sender.parked_for_decision")
                    await gate.wait()
                    log.info("glass_sender.resumed")
        except asyncio.CancelledError:
            pass

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
        # Card-source guarantee (Zack 2026-05-31): every card must be attributed.
        # emit_card sets `source`; this backstops any future bypass so a card is
        # never shipped blank.
        if kind == "card" and not frame.get("source"):
            frame["source"] = "Cortex"
            log.warning("card.missing_source", cmd_id=frame.get("cmd_id"))
        # A decision card (wearer must act) parks the sender until decided, so
        # trailing progress can't bury it; notification (options=[]) does not.
        blocks = kind == "card" and payload.get("card_type") in (
            "checkpoint", "question", "stt_review",
        )
        self._glass_send(json.dumps(frame, ensure_ascii=False), blocks=blocks)
        log.info("glass_frame.emit", kind=kind, blocks=blocks)

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
        source: str,
        scroll_total_lines: int = 0,
        options: list[str] | None = None,
        ttl_ms: int = 30_000,
        echo: str | None = None,
        card_type: str | None = None,
    ) -> None:
        from .markdown_runs import to_runs
        # `options is None` means "no caller preference — use the canonical
        # approve/modify/kill". An explicit `options=[]` means "info-only card,
        # no actionable buttons" — must be preserved as empty (don't fall
        # through to the truthiness default).
        resolved_options = ["approve", "modify", "kill"] if options is None else options
        frame: dict[str, Any] = {
            "cmd_id": cmd_id,
            "title_runs": to_runs(title),
            "body_runs": to_runs(body_md),
            "scroll_total_lines": scroll_total_lines,
            "options": resolved_options,
            # Piece 4: explicit card type so Glass renders + ring-maps cleanly
            # (notification=dismiss · checkpoint=approve/modify/reject · question=answer
            #  · stt_review=approve/modify). Caller may override the derived type.
            "card_type": card_type or _card_type_for(resolved_options),
            "source": source,
            "ttl_ms": ttl_ms,
        }
        # "Quote + body" layout (Zack 2026-05-30): when we know what triggered
        # this card — the wearer's own words, or the upstream tool's ask — pass
        # it as `echo` so Glass renders a dim quoted row above the title. Glass
        # degrades gracefully (no row) when absent.
        if echo and echo.strip():
            frame["echo_runs"] = to_runs(echo.strip())
        await self._emit_glass_frame("card", frame)

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
        tier: str = "standard",    # 'standard' (1080p) | 'detail' (2K) — the
        # glasses map the tier name → (long-edge px, jpeg quality).
        timeout_s: float = 18.0,   # CameraGate warmup+capture measured ~10.6–11.4s
        # on-device (2026-05-30); 10s clipped valid frames. 18s gives margin.
    ) -> str | None:
        """Ask the current glass peer to capture a photo and send it back.
        Returns the b64-encoded JPEG string on success, or None on timeout /
        peer-not-glass / peer-missing-capability.

        Used at the top of `_handle_user_invoke` when the 「视觉」 vision cue fired
        but the originating event had no image attached.
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
            "tier": tier,
        }
        if hint:
            payload["hint"] = hint
        await self._emit_glass_frame("request_image", payload)
        log.info("request_image.sent", req_id=req_id, parent=parent_event_id, hint=hint, tier=tier)
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
        # New transport: a header frame with binary:true announces that the raw
        # JPEG bytes follow as the next binary WS frame. Stash the req_id and
        # wait — _handle_binary_image_frame resolves the future. (Legacy
        # base64-in-JSON below still works for glass builds that send `image`.)
        if payload.get("binary"):
            self._pending_binary_image = (req_id, payload.get("mime") or "image/jpeg")
            log.info("image_attached.binary_header", req_id=req_id,
                     mime=payload.get("mime"), bytes_len=payload.get("bytes_len"))
            return
        # Empty string is treated as "tried but no usable image" — None signals
        # the same downstream (fallback to image-less dispatch).
        result: str | None = image_b64 if image_b64 else None
        # Archive EVERY incoming photo, used or not (Zack 2026-06-01) — legacy
        # base64-in-JSON path (binary path archives in _handle_binary_image_frame).
        if result:
            import base64 as _b64
            try:
                archived = _archive_capture(_b64.b64decode(result), req_id)
            except Exception:
                archived = None
            if archived:
                log.info("capture.archived", req_id=req_id,
                         file=os.path.basename(archived))
        fut.set_result(result)
        log.info(
            "image_attached.resolved",
            req_id=req_id, has_image=bool(result),
            bytes_b64=len(result) if result else 0,
        )

    async def _handle_binary_image_frame(self, data: bytes) -> None:
        """A binary WS frame = the raw JPEG bytes for the upload announced by
        the preceding `image_attached` header frame. Pair it with that req_id,
        base64-encode (the Future's contract + the Claude content block both
        want base64), and resolve. Cortex keeps this exact (1568px/q90) image
        as Zack's stored copy — no on-server re-compress."""
        pending = self._pending_binary_image
        self._pending_binary_image = None
        if pending is None:
            log.warning("binary_image.no_header", bytes=len(data))
            return
        req_id, _mime = pending
        # Archive EVERY incoming photo to disk, used or not (Zack 2026-06-01) —
        # before the pending-future check, so even an orphan frame is kept.
        archived = _archive_capture(data, req_id)
        if archived:
            log.info("capture.archived", req_id=req_id,
                     file=os.path.basename(archived), bytes=len(data))
        fut = self._pending_image_requests.get(req_id)
        if fut is None or fut.done():
            log.warning("binary_image.no_pending_or_done", req_id=req_id)
            return
        import base64 as _b64
        result: str | None = _b64.b64encode(data).decode("ascii") if data else None
        fut.set_result(result)
        log.info("binary_image.resolved", req_id=req_id,
                 bytes=len(data), has_image=bool(result))

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
                self._glass_send(cmd.model_dump_json())
            except Exception as e:
                log.warning("command.send_failed", id=cmd.id, kind=cmd.kind, error=str(e))
                return
        # Glass-shaped frame, if the peer wants one. Pull the wearer's own words
        # (when this card traces back to a user_invoke) so emit_card can render
        # the "quote + body" layout. Cards with no user utterance behind them
        # (some reverse-wake paths) get echo="" → no quote row (graceful).
        _pending = self._pending_previews.get(cmd.id)
        _ev = _pending.get("event") if _pending else None
        echo_text = (getattr(_ev, "payload", None) or {}).get("text") or "" if _ev else ""
        if cmd.kind == "preview_action":
            options = cmd.payload.get("options") or []
            await self.emit_card(
                cmd_id=cmd.id,
                title=cmd.payload.get("title", ""),
                body_md=cmd.payload.get("body", ""),
                options=options,
                source=cmd.payload.get("source") or "Cortex",
                ttl_ms=cmd.ttl_ms,
                echo=echo_text,
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
                    source=cmd.payload.get("source") or "Cortex",
                    ttl_ms=cmd.ttl_ms,
                    echo=echo_text,
                )

    # ── P0.1: HUD-session-scoped CC tmux registry ─────────────────────────
    # TTL: a tmux idle for more than this is considered stale and gets
    # evicted on the next reuse attempt (cold-start preferred over an
    # attention-diluted long-running CC session). 30 min is plenty for an
    # active conversation; longer and the user is likely on a new topic.
    _HUD_TMUX_TTL_S = 1800.0

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
        source: str = "Cortex",
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
            source=source,
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
            "tier": cfg["tier"],   # capture tier for photo-bearing fires
        })
        log.info(
            "shortcut_config.emitted",
            slot=cfg["slot"], send_photo=cfg["send_photo"], label=cfg["label"],
            tier=cfg["tier"],
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
        # P1 (SDK single-source): resume the ARCHIVED session via the SDK
        # (resume=cc_sid) instead of a tmux `claude --resume` respawn. This is
        # spec point 8's "find a past session and continue it" through one
        # ordered stream + native permission gate.
        if _use_sdk_agent():
            from .claude_sdk_agent import SdkAgentSession
            rpc_result = await SdkAgentSession(
                self, event, brief=instruction,
                schema_hint=CANONICAL_ACTIONS_SCHEMA, add_dirs=add_dirs,
                working_dir=working_dir, permission_mode=_permission_mode_for(instruction),
                timeout_s=240.0, resume_session_id=cc_sid,
            ).run()
            if not rpc_result or not rpc_result.get("ok", True):
                await self._emit_info_card(
                    event_id=event.id, title="Couldn't resume",
                    body=f"That session couldn't be resumed ({(rpc_result or {}).get('error', 'unknown')}).",
                    ttl_ms=8_000)
                return
            await self._send_agent_card_for_decision(rpc_result, event, working_dir, 240.0)
            log.info("session_browse.resumed_sdk", cc_sid=cc_sid[:8])
            return

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
            source="Cortex",
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

        # ── P1 (SDK single-source): modify-on-FINAL = resume the prior session
        # via the SDK with a modify brief. Skips the tmux reuse / --resume logic. ─
        if _use_sdk_agent():
            from .claude_sdk_agent import SdkAgentSession
            await self._emit_progress_to_glass(
                parent_event_id=original_event.id, stage="resuming_agent", icon="✍️",
                detail=f"resuming session {cc_session_id[:8]} with your correction",
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
            rpc_result = await SdkAgentSession(
                self, original_event, brief=brief,
                schema_hint=CANONICAL_ACTIONS_SCHEMA, add_dirs=add_dirs,
                working_dir=working_dir, permission_mode=_permission_mode_for(modify_text),
                timeout_s=timeout_s, resume_session_id=cc_session_id,
            ).run()
            await self._send_agent_card_for_decision(
                rpc_result or {}, original_event, working_dir, timeout_s)
            return

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

        # P1 (SDK single-source): resume the prior session with the user's text
        # (continue, or a checkpoint correction) instead of pasting into tmux.
        if _use_sdk_agent():
            from .claude_sdk_agent import SdkAgentSession
            from .agent_brief import CANONICAL_ACTIONS_SCHEMA
            if not cc_session_id:
                log.warning("agent.resume_phase.no_session_sdk")
                return
            await self._emit_progress_to_glass(
                parent_event_id=original_event.id, stage="resuming_agent", icon="✍️",
                detail=f"resuming session {cc_session_id[:8]}",
            )
            rpc_result = await SdkAgentSession(
                self, original_event, brief=user_text,
                schema_hint=CANONICAL_ACTIONS_SCHEMA,
                add_dirs=[os.path.expanduser("~/constellation/twin"),
                          os.path.expanduser("~/Code/Projects"),
                          os.path.expanduser("~/.claude/projects")],
                working_dir=working_dir, permission_mode=_permission_mode_for(user_text),
                timeout_s=timeout_s, resume_session_id=cc_session_id,
            ).run()
            await self._send_agent_card_for_decision(
                rpc_result or {}, original_event, working_dir, timeout_s)
            return

    async def _send_agent_card_for_decision(
        self, rpc_result: dict, event: Event, working_dir: str | None, timeout_s: float,
    ) -> None:
        """Same card logic as http._send_agent_card. Kept here so the
        user_decision path doesn't need to round-trip through http module."""
        from .schema import Command
        structured = rpc_result.get("structured") if isinstance(rpc_result.get("structured"), dict) else None
        is_checkpoint = _is_checkpoint(structured) or bool(rpc_result.get("is_checkpoint"))
        # Card-source: complex path = the Claude agent (Vision if a photo rode in).
        agent_source = "Claude Vision" if (event.payload or {}).get("image") else "Claude"

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
                    "source": agent_source,
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
            # No actions → this is an ANSWER (query/lookup), not a proposal.
            # Show the agent's reply AS the body and DROP the old long
            # "Agent finished — no actions" header (Zack 2026-05-30: low info,
            # too long). Prefer structured.summary (clean prose) over raw
            # result_text (which may be the serialized JSON blob).
            summary = (structured or {}).get("summary") if isinstance(structured, dict) else None
            notes = (structured or {}).get("notes") if isinstance(structured, dict) else None
            if summary:
                body_md = summary if not notes else f"{summary}\n\n— {notes}"
                title = ""  # the answer IS the body; no redundant header
            else:
                raw = (rpc_result.get("result_text") or "").strip()
                if raw and not raw.lstrip().startswith("{"):
                    body_md = raw      # plain final text → show it as the answer
                    title = ""
                else:
                    body_md = "（这次没有产生结果)"
                    title = "没有结果"
            body_md = body_md[:1500]

        cmd = Command(
            id=ids.command_id(), ts=datetime.now(timezone.utc),
            kind="preview_action",
            payload={
                "title": title, "body": body_md[:2000], "icon": "✦",
                "options": (list(_THREE_OPTIONS) if subtasks else []),
                "source": agent_source,
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

        # UC1: if a photo rode in with this turn, persist it under twin/ now (so
        # the agent can embed it in a memo, and Zack keeps a copy) and hand the
        # agent the relative path. The image itself rides INTO the agent as a
        # content block (Claude is natively multimodal) — there is NO separate
        # image→text pre-step. The agent SEES the photo and decides, from Zack's
        # prompt, what to do with it (memo, read text off it, answer about it).
        photo_path: str | None = None
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

        brief = build_agent_brief(
            ask_text=ask_text,
            now_iso=datetime.now(timezone.utc).astimezone().isoformat(),
            has_photo=bool(payload.get("image")),
            photo_path=photo_path,
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

        # The complex agent runs in-process on the Claude Agent SDK single
        # source (claude_sdk_agent). The retired tmux dual-worker dispatch is
        # gone — _use_sdk_agent() always returns True, so this is the only path.
        if _use_sdk_agent():
            from .claude_sdk_agent import SdkAgentSession
            if sid:
                self.sessions.append(
                    sid, "agent_dispatch", event_id=event.id,
                    brief_chars=len(brief), add_dirs=add_dirs,
                    timeout_s=timeout_s, via="sdk",
                )
            rpc_result = await SdkAgentSession(
                self, event, brief=brief, schema_hint=schema_hint,
                add_dirs=add_dirs, working_dir=working_dir,
                permission_mode=permission_mode, timeout_s=timeout_s,
                # UC1: the photo rides into the agent as a multimodal content
                # block so Claude sees it directly (no vision_describe pre-step).
                image_b64=(event.payload or {}).get("image"),
                # UC2 / modify-on-final: continue a prior CC session when the
                # caller supplied one (same payload key the tmux path uses).
                resume_session_id=(event.payload or {}).get("resume_cc_session_id"),
            ).run()
            if sid and rpc_result:
                self.sessions.append(
                    sid, "agent_completed", event_id=event.id,
                    cc_session_id=rpc_result.get("session_id"),
                    n_tool_uses=rpc_result.get("n_tool_uses"),
                    terminate_reason=rpc_result.get("terminate_reason"),
                    is_checkpoint=rpc_result.get("is_checkpoint"), via="sdk",
                )
            await self._send_agent_card_for_decision(
                rpc_result or {}, event,
                working_dir=working_dir, timeout_s=timeout_s,
            )
            return

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
        # downstream `_dispatch_complex_agent` then runs the SDK agent for that
        # session (resuming the prior CC session when one is supplied).
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

        # Vision capture (2026-05-31): the deterministic cue 「视觉」 / "vision"
        # (see _looks_visual_intent / _VISION_KEYWORD_PATTERN) means Zack wants a
        # frame. Pull a scene capture from glass NOW — before classifier + router —
        # so the photo rides into whichever path runs (planner as a multimodal
        # image block, or the agent as a content block). One capture, hands the
        # image unchanged downstream; never reduced to text.
        # Skipped when no glass peer is connected (capture needs the WSS) or when
        # the event already carries an image (e.g. a photo shortcut).
        if (
            ask_text
            and not payload.get("image")
            and self._glass_conn
            and "card" in self._glass_accept
            and _looks_visual_intent(ask_text)
        ):
            tier = _vision_tier_for(ask_text)
            log.info("vision.upfront_request_image", event_id=event.id,
                     ask_preview=ask_text[:60], tier=tier)
            await self._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="capturing", icon="📷",
                detail="capturing scene…" if tier == "standard" else "capturing scene (hi-res)…",
            )
            img = await self._request_image_from_glass(
                parent_event_id=event.id,
                hint="scene in front",
                tier=tier,
            )
            if img:
                event.payload["image"] = img
                log.info("vision.upfront_image_attached", event_id=event.id, bytes_b64=len(img))
            else:
                log.warning(
                    "vision.upfront_image_unavailable_fallback_textonly",
                    event_id=event.id,
                )

        # Deterministic model pin (Zack 2026-06-01): if the ask NAMES a model,
        # honour it and SKIP the classifier — no LLM guess. 'claude' → the agent
        # path; 'gpt' → the router path. (The vision capture above already ran if
        # a vision cue was present — the model pin is orthogonal to it.)
        _model_pin = _model_override_for(ask_text)
        if _model_pin == "claude":
            log.info("intent.model_override", forced="claude")
            await self._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="classified", icon="🧭",
                detail="model: Claude (you named it) → agent", meta="Claude",
            )
            await self._dispatch_complex_agent(event)
            return
        if _model_pin == "gpt":
            log.info("intent.model_override", forced="gpt")
            await self._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="classified", icon="🧭",
                detail="model: GPT (you named it) → planner", meta="GPT-5.2",
            )

        # Phase 5c — classify intent first; complex asks bypass the v0.5
        # Router entirely and go straight to the CC agent path. Simple
        # asks (single-step state queries or explicit one-action requests)
        # continue through the existing planner + executor-adapter dispatch.
        if _model_pin != "gpt" and not self.use_stub_router:   # stub router (Phase 1) is for tests only
            from .classifier import classify_intent
            await self._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="classifying", icon="🧭",
                detail="classifying intent", meta="GPT-5.2",
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
                        detail=f"intent: complex → agent — {why}", meta="GPT-5.2",
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
                    detail=f"intent: simple → planner — {why}", meta="GPT-5.2",
                )
            except Exception as e:
                log.warning("classifier.errored_falling_through", error=str(e))
                # Fall through to existing path on classifier failure

        await self._emit_progress_to_glass(
            parent_event_id=event.id,
            stage="planning", icon="🧠",
            detail="planning dispatch (router)", meta="GPT-5.2",
        )
        plan = await self._route(event)
        log.info("plan.generated", primary_intent=plan["primary_intent"])
        await self._emit_progress_to_glass(
            parent_event_id=event.id,
            stage="planned", icon="🎯",
            detail=f"plan: {plan['primary_intent']} · {len(plan.get('subtasks', []))} subtasks",
        )

        # Router occasionally slips kind="tool_card" for normal user_invoke; that
        # kind is a legacy reverse-wake shape we no longer emit. Normalize to
        # preview_action so the SEND gate still applies and confirm-policies fire
        # below.
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
                self._glass_send(denied_card.model_dump_json())
            self._write_receipt(plan, [{}] * len(plan["subtasks"]), event.id)
            return

        hud_kind = plan["hud_response"]["kind"]

        # Subtask dispatch strategy depends on whether HUD requires user confirm:
        #   - preview_action: dispatch draft/query now (for preview); defer execute to SEND.
        #   - hud_show:       confirm-policy says auto. Dispatch ALL subtasks now so the
        #                     hud_show body can reflect real results; receipt written immediately.
        #
        # Vision (2026-05-31): if a frame was captured for this turn (the 「视觉」
        # cue fired at the top of _handle_user_invoke), it already rode into the
        # planner as a multimodal image block (router.route()) — the planner read
        # it to answer or to fill an adapter's args. Adapters never receive image
        # bytes; there is no image→text tool, so nothing to inject here.
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
                rpc_result = await self._dispatch_to_tool({**st, "args": args})
                subtask_results.append(rpc_result.result)
            else:
                subtask_results.append({})  # placeholder so indices align

        await self._emit_progress_to_glass(
            parent_event_id=event.id,
            stage="preparing_card", icon="🎴",
            detail=f"preparing {hud_kind}",
        )
        cmd = self._build_command(plan, subtask_results,
                                 "GPT Vision" if (event.payload or {}).get("image") else "GPT")
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

    def _build_command(
        self, plan: dict[str, Any], results: list[dict[str, Any]],
        source: str = "GPT",
    ) -> Command:
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
                "source": source,
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
        # The wearer decided → open the sender gate so any progress queued behind
        # the decision card flows again (see _glass_sender_loop). Safe to set on
        # every decision: an already-open gate is a no-op.
        if self._decision_gate is not None:
            self._decision_gate.set()

        # P1 — SDK permission/question cards resolve an in-process future
        # (see claude_sdk_agent). Drain before the tmux/preview paths since
        # their cmd_ids live in a separate registry (_sdk_pending). modify/answer
        # without text re-opens the mic and keeps the card pending. No-op (returns
        # False) when this cmd_id isn't an SDK card.
        if self._sdk_pending and cmd_id in self._sdk_pending:
            from .claude_sdk_agent import resolve_sdk_decision
            if await resolve_sdk_decision(self, cmd_id, decision, feedback_text):
                return

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

        # STT-review gate (Zack 2026-05-30): this cmd_id may be an "STT review"
        # card. approve → route the reviewed transcript per its original intent
        # (the ONLY place raw STT moves downstream); modify (LONG) → re-open the
        # mic to say it again. Drained before _pending_previews so a review card
        # never falls through to the generic preview handler.
        stt = self._pending_stt_review.pop(cmd_id, None)
        if stt:
            d = (decision or "").strip().lower()
            if d == "approve":
                # ONLY an explicit approve routes the transcript downstream.
                log.info("stt_review.approved", cmd_id=cmd_id, intent=stt.get("intent"))
                await self._route_stt_approved(stt)
            elif d == "modify":
                # 重讲 (LONG): re-open the right mic, preserving intent. ('redo'
                # token purged 2026-06-01 — LONG is uniformly 'modify'.)
                log.info("stt_review.respeak", cmd_id=cmd_id, intent=stt.get("intent"))
                await self._respeak_stt(stt)
            else:
                # kill (double-tap) / reject / anything else → TERMINATE the flow.
                # The transcript is dropped (NEVER routed) and the pending entry is
                # already popped, so the flow ENDS cleanly here. We do NOT fall
                # through to "approve" — an unrecognized decision must never
                # silently send raw STT. (Zack 2026-05-30: a flow ends only as
                # approve=routed or kill=terminated; no dismiss, no dangling.)
                log.info("stt_review.killed", cmd_id=cmd_id,
                         decision=d, intent=stt.get("intent"))
            return

        # Peek (don't pop yet) — we may need to re-register if Modify lacks text.
        pending = self._pending_previews.get(cmd_id)
        if not pending:
            log.warning("user_decision.no_pending", cmd_id=cmd_id, known=list(self._pending_previews.keys()))
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
            # SDK path: a kill on a FINAL/checkpoint card has nothing to tear down
            # (the in-process run already ended at its ResultMessage); a mid-run
            # kill arrives on a permission card and is handled by
            # resolve_sdk_decision → PermissionResultDeny(interrupt). (tmux
            # agent_kill retired, Rev 18 C-72.)
            # P0.1 — drop registry entry so the next invoke in this HUD
            # session spawns fresh (Kill is an explicit reset signal).
            if sid:
                self._active_hud_session_tmux.pop(sid, None)
            if self._glass_conn:
                self._glass_send(json.dumps({
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
                self._glass_send(json.dumps({
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
        cmd = self._build_command(next_plan, subtask_results,
                                 "GPT Vision" if (original_event.payload or {}).get("image") else "GPT")
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
    # P2.3 — TCC self-check. Runs in background; if any Apple app is denied,
    # stash a hud_show that fires on the next Glass connect.
    from .tcc_check import run_and_surface as _tcc_run_and_surface
    asyncio.create_task(_tcc_run_and_surface(server))
    log.info("cortex.listening", host=host, port=port)
    # max_size 8 MB: the default 1 MB rejects the binary image upload (1568px/q90
    # photos run ~0.4–1 MB; base64-in-JSON fallback inflates +33%). Headroom for
    # both transports without ever tripping "message too big" on a detailed scene.
    async with websockets.serve(server.handle_glass, host, port,
                                max_size=8 * 1024 * 1024):
        await asyncio.Future()  # run forever
