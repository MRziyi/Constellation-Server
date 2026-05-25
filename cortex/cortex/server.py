"""WebSocket server — Glass-facing endpoint.

Phase 2 Slice A: real Router (when OPENAI_API_KEY present) or stub fallback.
Phase 3+: full Hybrid Connection Model (see INTERFACE-CONTRACTS.md §1.6) with push wake.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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
    """Parse twin/skills/confirm-policies.md → {tool:action → policy}.

    Looks for the YAML block in the file, then extracts lines like:
      applescript_mail:send         : preview-always
      fs:read                       : auto

    Returns empty dict if file missing — caller falls back to 'preview-default'.
    """
    import re
    rules: dict[str, str] = {}
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

    Additional enforcement (SoT C-20 + C-22): any plan with task_continues=true MUST
    have hud_kind=preview_action so the user has a yield point to optionally voice-feedback
    via the always-on mic. Auto-advancing past a hud_show would silently violate C-22.

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

    needs_user_gate = forced_preview or bool(plan.get("task_continues"))
    if needs_user_gate and plan.get("hud_response", {}).get("kind") == "hud_show":
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


# ── Two-option decision canonicalization (Zack 2026-05-25) ────────────────
# Every blocking card has exactly two buttons: Approve / Modify.
# Free-text on the feedback channel (typed in the composer, or spoken via
# STT on Glass) gets classified into the same two outcomes server-side so
# the user doesn't have to click — saying "ok, go" is approve; saying
# anything substantive is modify-with-content.
#
# Approve = proceed exactly as previewed (send / continue / execute / yes).
# Modify  = redirect with details (the text is the redirection).
# Cancel/dismiss does NOT exist as a button anymore — to abandon, the user
# can Modify with "cancel this" or just ignore the card until ttl fires.
_TWO_OPTIONS = ["Approve", "Modify"]

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


def _classify_user_decision(
    decision: str | None,
    feedback_text: str | None,
) -> tuple[str, str | None]:
    """Return ('approve' | 'modify', resolved_text_for_modify_or_None).

    - Button click: `decision` is the button label. Map via the token sets.
      A button-click "Modify" with empty feedback_text → ('modify', None);
      caller must re-surface the card.
    - Free text: `decision` may be a generic marker like "feedback" or "send"
      while `feedback_text` carries the actual content. The free-text content
      itself is checked for approve-like phrases.
    """
    d = (decision or "").strip().lower()
    if d in _APPROVE_BUTTON_TOKENS:
        return "approve", None
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
            }
        )

        # Parse confirm-policies once at construction; reload on Twin write later (Phase 7).
        self._confirm_policies = _parse_confirm_policies(twin.root)
        log.info(
            "confirm_policies.loaded",
            count=len(self._confirm_policies),
            rules=sorted(self._confirm_policies.keys()) if len(self._confirm_policies) < 40 else "<truncated>",
        )

    # ── Glass-side handler ──

    async def handle_glass(self, ws: ServerConnection) -> None:
        log.info("glass.connected", remote=ws.remote_address)
        self._glass_conn = ws
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
                    "options": list(_TWO_OPTIONS),
                },
                requires_confirm=True, ttl_ms=600_000,
            )
            self._pending_previews[cmd.id] = {
                "event": event,
                "plan": {
                    "primary_intent": "agent_checkpoint",
                    "subtasks": [], "reasoning": "phase checkpoint",
                    "hud_response": cmd.payload, "task_continues": True,
                },
                "subtask_results": [], "task_history": [],
                "from_agent": True, "is_checkpoint": True,
                "agent_result": rpc_result,
                "agent_working_dir": working_dir,
                "agent_timeout_s": timeout_s,
            }
            if self._glass_conn:
                await self._glass_conn.send(cmd.model_dump_json())
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
            body_md = (rpc_result.get("result_text") or "(no actions proposed)")[:1500]
            title = "Agent finished — no actions"

        cmd = Command(
            id=ids.command_id(), ts=datetime.now(timezone.utc),
            kind="preview_action",
            payload={
                "title": title, "body": body_md[:2000], "icon": "✦",
                "options": (list(_TWO_OPTIONS) if subtasks else []),
            },
            requires_confirm=bool(subtasks), ttl_ms=300_000,
        )
        self._pending_previews[cmd.id] = {
            "event": event,
            "plan": {
                "primary_intent": "agent_actions",
                "subtasks": subtasks, "reasoning": "agent dispatch (resumed)",
                "hud_response": cmd.payload, "task_continues": False,
            },
            "subtask_results": [{} for _ in subtasks],
            "task_history": [],
            "from_agent": True,
            "agent_result": rpc_result,
        }
        if self._glass_conn:
            await self._glass_conn.send(cmd.model_dump_json())

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

        brief = build_agent_brief(
            ask_text=ask_text,
            now_iso=datetime.now(timezone.utc).astimezone().isoformat(),
            has_photo=bool(payload.get("image")),
            twin_slices=twin_slices,
            output_schema=schema_hint,
            available_dirs=add_dirs,
        )
        await self._emit_progress_to_glass(
            parent_event_id=event.id,
            stage="brief_assembled", icon="📝",
            detail=f"agent brief assembled ({len(brief)} chars)",
        )

        # Dispatch the agent action. RPC returns when CC end_turn's
        # (checkpoint or final).
        try:
            await self._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="dispatching_agent", icon="🤖",
                detail="dispatching claude_code.agent",
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
                },
                "result_format": "execute",
            })
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
        option_labels = [opt.get("label", opt.get("id", "?")) for opt in options]
        wake_response_map: dict[str, dict[str, Any]] = {}
        for opt in options:
            opt_id = opt.get("id")
            if not opt_id:
                continue
            if from_tool == "claude_code" and wake_kind == "permission_request":
                key_seq: list[str] | None = None
                if opt_id == "allow_once":
                    key_seq = ["Enter"]
                elif opt_id == "allow_always":
                    key_seq = ["Down", "Enter"]
                elif opt_id == "deny":
                    key_seq = ["Down", "Down", "Enter"]
                if key_seq:
                    wake_response_map[opt_id] = {
                        "tool": "claude_code", "action": "send_keys",
                        "args": {"session_id": session_id, "keys": key_seq, "literal": False},
                        "context_pack": [], "result_format": "execute",
                    }

        title_map = {
            "permission_request": f"{from_tool} needs you",
            "completion_notice": f"{from_tool} done",
            "error": f"{from_tool} error",
            "surprising_event": from_tool,
        }
        icon_map = {
            "permission_request": "⚙",
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
        }
        if self._glass_conn:
            await self._glass_conn.send(cmd.model_dump_json())
        log.info("command.sent", id=cmd.id, kind=cmd.kind, source="reverse_wake")

        # If there's no actionable choice (e.g. completion_notice), this is a one-shot
        # info card; write the receipt now.
        if not options:
            self._pending_previews.pop(cmd.id, None)
            self._write_receipt(synthetic_plan, [], event.id)

    async def _handle_user_invoke(self, event: Event) -> None:
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
                if decision.get("complex"):
                    log.info("intent.complex_via_agent", why=decision.get("why"))
                    await self._emit_progress_to_glass(
                        parent_event_id=event.id,
                        stage="classified", icon="🧭",
                        detail=f"intent: complex → agent — {why}",
                    )
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
        cmd = self._build_command(plan, subtask_results)
        self._pending_previews[cmd.id] = {
            "event": event,  # full event kept so we can re-route on feedback / advance
            "plan": plan,
            "subtask_results": subtask_results,
            "task_history": [],  # multi-step task history; empty on first round
        }
        if self._glass_conn:
            await self._glass_conn.send(cmd.model_dump_json())
        log.info(
            "command.sent",
            id=cmd.id, kind=cmd.kind,
            task_continues=bool(plan.get("task_continues")),
            n_history=0,
        )

        # hud_show = "done already, just informing"; no user gate, write receipt now.
        # (If task_continues=true with hud_show, this is an unusual case — auto-advance.)
        if hud_kind == "hud_show":
            self._pending_previews.pop(cmd.id, None)
            self._write_receipt(plan, subtask_results, event.id)
            if plan.get("task_continues"):
                # Auto-advance with the hud_show results as a step in history.
                history = [self._summarize_step_for_history(plan, subtask_results, "auto_advance")]
                await self._advance_task(event, history, event.id)

    async def _route(
        self,
        event: Event,
        feedback_iteration: dict[str, Any] | None = None,
        task_history: list[dict[str, Any]] | None = None,
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
            task_history=task_history,
        )
        context_pack = self.twin.assemble_context_pack(picked)
        return await route(
            event=event,
            available_tools_block=self._tools_block,
            allowed_tools=self._allowed_tools,
            model=self.router_model,
            context_pack=context_pack,
            feedback_iteration=feedback_iteration,
            task_history=task_history,
        )

    def _build_command(self, plan: dict[str, Any], results: list[dict[str, Any]]) -> Command:
        hud = plan["hud_response"]
        body = self._interpolate(hud["body_template"], results, plan=plan)
        # Two-option contract (Zack 2026-05-25): every blocking card has
        # exactly Approve / Modify; info cards (hud_show) have no buttons.
        # Whatever the router emitted under hud_response.options is ignored.
        kind = hud["kind"]
        if kind == "preview_action":
            options: list[str] = list(_TWO_OPTIONS)
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

    MAX_TASK_ROUNDS = 5

    async def _handle_user_decision(self, event: Event) -> None:
        decision = event.payload.get("decision")
        cmd_id = event.payload.get("in_reply_to")
        feedback_text = event.payload.get("feedback_text")
        log.info("user_decision.received", decision=decision, cmd_id=cmd_id, has_feedback=bool(feedback_text))

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

        # "dismiss" is a non-UI signal — fired by the web client on TTL
        # expiry or by programmatic callers. Drops the pending card and
        # kills any active agent tmux session. No button surfaces it.
        if (decision or "").strip().lower() == "dismiss":
            self._pending_previews.pop(cmd_id, None)
            log.info("dismissed", cmd_id=cmd_id)
            if pending.get("is_checkpoint") and pending.get("agent_result"):
                tmux_session = pending["agent_result"].get("tmux_session")
                if tmux_session:
                    try:
                        await self._dispatch_to_tool({
                            "tool": "claude_code", "action": "agent_kill",
                            "args": {"tmux_session": tmux_session},
                            "result_format": "execute",
                        })
                    except Exception as e:
                        log.warning("agent_kill.failed", error=str(e))
            return

        # Canonicalize: every blocking card has exactly two outcomes —
        # approve (proceed as previewed) or modify (redirect with text).
        # Free-text on the feedback channel is classified by content.
        kind, resolved_text = _classify_user_decision(decision, feedback_text)
        log.info(
            "decision.classified",
            kind=kind,
            has_text=bool(resolved_text),
            from_button=bool(decision and decision.strip().lower() in (_APPROVE_BUTTON_TOKENS | _MODIFY_BUTTON_TOKENS)),
        )

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
                    "detail": "Modify clicked — please tell me how to change it.",
                }, ensure_ascii=False))
            return  # leave the card pending

        # ── Multi-phase agent checkpoint: resume CC with the canonical outcome ──
        if pending.get("is_checkpoint") and pending.get("agent_result"):
            self._pending_previews.pop(cmd_id, None)
            _append_learning_signal(
                self.twin, event=pending["event"], pending=pending,
                decision_kind=kind,
                correction_text=resolved_text,
            )
            await self._resume_agent_phase(pending, decision, feedback_text, event)
            return

        # Commit: consume the pending entry now.
        self._pending_previews.pop(cmd_id, None)

        # Implicit-learning signal: append this decision to the learning
        # queue. Both Approve (positive signal) and Modify (correction
        # signal) are valuable training data for Phase-7 skill distillation.
        _append_learning_signal(
            self.twin, event=pending["event"], pending=pending,
            decision_kind=kind,
            correction_text=resolved_text,
        )

        plan = pending["plan"]
        task_continues = bool(plan.get("task_continues"))
        task_history: list[dict[str, Any]] = list(pending.get("task_history") or [])
        original_event: Event = pending["event"]

        if kind == "approve":
            if task_continues:
                exec_subtasks = [st for st in plan["subtasks"] if st["result_format"] == "execute"]
                if exec_subtasks:
                    log.warning("intermediate.has_execute_subtasks", n=len(exec_subtasks))
                    await self._execute_remaining_no_receipt(pending)
                self._write_step_receipt(plan, pending["subtask_results"], event.id, step_index=len(task_history))
                task_history.append(self._summarize_step_for_history(plan, pending["subtask_results"], "send"))
                try:
                    await self._advance_task(original_event, task_history, event.id)
                except Exception as e:
                    log.error("task.advance_failed", error=str(e), error_type=type(e).__name__, exc_info=True)
                    raise
            else:
                try:
                    await self._execute_remaining(pending, event.id)
                except Exception as e:
                    log.error("execute_remaining.failed", error=str(e), error_type=type(e).__name__, exc_info=True)
                    raise
            return

        # kind == "modify" with text
        task_history.append(self._summarize_step_for_history(
            plan, pending["subtask_results"], "feedback", resolved_text
        ))
        try:
            await self._advance_task(
                original_event, task_history, event.id,
                feedback_text=resolved_text,
                prior_plan=plan,
            )
        except Exception as e:
            log.error("feedback_loop.failed", error=str(e), error_type=type(e).__name__, exc_info=True)
            raise

    def _summarize_step_for_history(
        self,
        plan: dict[str, Any],
        subtask_results: list[dict[str, Any]],
        user_decision: str,
        feedback_text: str | None = None,
    ) -> dict[str, Any]:
        """Build a compact step record for inclusion in PRIOR TASK HISTORY."""
        return {
            "step_intent": plan.get("primary_intent"),
            "next_step_hint": plan.get("next_step_hint"),
            "subtasks": [
                {
                    "tool": s.get("tool"),
                    "action": s.get("action"),
                    "result_format": s.get("result_format"),
                    "args": s.get("args"),
                }
                for s in plan.get("subtasks", [])
            ],
            "subtask_results": subtask_results,
            "router_reasoning": plan.get("reasoning"),
            "user_decision": user_decision,
            "user_feedback_text": feedback_text,
        }

    async def _execute_remaining_no_receipt(self, pending: dict[str, Any]) -> None:
        """Same as _execute_remaining but without writing the final receipt — used when
        we know we're in the middle of a multi-step task and want to keep going."""
        plan = pending["plan"]
        results = list(pending["subtask_results"])
        for i, st in enumerate(plan["subtasks"]):
            if st["result_format"] == "execute":
                interpolated_args = self._interpolate_args(st.get("args", {}), results, plan=plan)
                rpc_result = await self._dispatch_to_tool({**st, "args": interpolated_args})
                results[i] = rpc_result.result
        pending["subtask_results"] = results

    def _write_step_receipt(
        self, plan: dict[str, Any], results: list[dict[str, Any]],
        src_evt: str, step_index: int,
    ) -> None:
        """Write a step-level receipt for a multi-step task (audit trail)."""
        rcpt_id = ids.receipt_id()
        body = (
            f"\n## {datetime.now(timezone.utc).strftime('%H:%M:%S')} — "
            f"[step {step_index}] {plan['primary_intent']} [{rcpt_id}]\n"
            f"- evt: {src_evt}\n"
            f"- task_continues: {plan.get('task_continues')}\n"
            f"- next_step_hint: {json.dumps(plan.get('next_step_hint'), ensure_ascii=False)[:200]}\n"
            f"- reasoning: {plan.get('reasoning')}\n"
        )
        for i, (st, r) in enumerate(zip(plan["subtasks"], results)):
            body += f"  - [{i}] {st['tool']}.{st['action']} ({st['result_format']}) → {json.dumps(r, ensure_ascii=False)[:160]}\n"
        self.twin.receipt_append(body)
        self.twin.changelog_append(
            summary=f"[step {step_index}] {plan['primary_intent']}",
            src=src_evt,
            details=[f"Appended step receipt {rcpt_id}"],
        )
        log.info("step_receipt", step_index=step_index, rcpt_id=rcpt_id)

    async def _advance_task(
        self,
        original_event: Event,
        task_history: list[dict[str, Any]],
        src_evt: str,
        feedback_text: str | None = None,
        prior_plan: dict[str, Any] | None = None,
    ) -> None:
        """Unified re-invocation path for both multi-step continuation and free-form feedback.

        Router sees PRIOR TASK HISTORY (always) + USER FEEDBACK (if present) and decides
        the next plan. This includes deciding whether to redo, advance, skip, or inject
        info per the FREE-FORM FEEDBACK INTERPRETATION section of the system prompt.
        """
        round_n = len(task_history)
        log.info("task.advance.start", round=round_n, has_feedback=bool(feedback_text))

        if round_n >= self.MAX_TASK_ROUNDS:
            log.warning("task.max_rounds_reached", rounds=round_n)
            terminal_cmd = Command(
                id=ids.command_id(), ts=datetime.now(timezone.utc),
                kind="hud_show",
                payload={
                    "title": "Task too long",
                    "body": f"Hit {self.MAX_TASK_ROUNDS}-step limit. Restate the request to start fresh.",
                    "icon": "✗", "options": [],
                },
                requires_confirm=False, ttl_ms=15_000,
            )
            if self._glass_conn:
                await self._glass_conn.send(terminal_cmd.model_dump_json())
            return

        feedback_iteration = None
        if feedback_text:
            feedback_iteration = {
                "feedback_text": feedback_text,
                "prior_plan_summary": (
                    {
                        "primary_intent": prior_plan["primary_intent"],
                        "reasoning": prior_plan.get("reasoning"),
                        "hud_response": prior_plan.get("hud_response"),
                    }
                    if prior_plan
                    else None
                ),
            }

        next_plan = await self._route(
            original_event,
            feedback_iteration=feedback_iteration,
            task_history=task_history,
        )
        next_plan = _apply_confirm_policies(next_plan, self._confirm_policies)
        log.info(
            "task.advanced",
            round=round_n,
            primary_intent=next_plan["primary_intent"],
            task_continues=bool(next_plan.get("task_continues")),
        )

        # Same dispatch flow as _handle_user_invoke
        hud_kind = next_plan["hud_response"]["kind"]
        subtask_results: list[dict[str, Any]] = []
        for st in next_plan["subtasks"]:
            if st["result_format"] in ("draft", "query") or hud_kind == "hud_show":
                args = self._interpolate_args(st.get("args", {}), subtask_results, plan=next_plan)
                rpc_result = await self._dispatch_to_tool({**st, "args": args})
                subtask_results.append(rpc_result.result)
            else:
                subtask_results.append({})

        cmd = self._build_command(next_plan, subtask_results)
        self._pending_previews[cmd.id] = {
            "event": original_event,
            "plan": next_plan,
            "subtask_results": subtask_results,
            "task_history": task_history,  # carry forward
        }
        if self._glass_conn:
            await self._glass_conn.send(cmd.model_dump_json())
        log.info(
            "command.sent",
            id=cmd.id, kind=cmd.kind,
            task_continues=bool(next_plan.get("task_continues")),
            n_history=round_n,
        )

        # If this round's HUD is hud_show, no user gate; write step receipt and possibly auto-advance.
        if hud_kind == "hud_show":
            self._pending_previews.pop(cmd.id, None)
            self._write_step_receipt(next_plan, subtask_results, src_evt, step_index=round_n)
            if next_plan.get("task_continues"):
                # Auto-advance — extend history with this round and recurse
                task_history.append(
                    self._summarize_step_for_history(next_plan, subtask_results, "auto_advance")
                )
                await self._advance_task(original_event, task_history, src_evt)

    async def _execute_remaining(self, pending: dict[str, Any], src_evt: str) -> None:
        """Run the `execute` subtasks after user SEND. Write receipt + CHANGELOG."""
        plan = pending["plan"]
        results = list(pending["subtask_results"])
        for i, st in enumerate(plan["subtasks"]):
            if st["result_format"] == "execute":
                # Re-interpolate args against accumulated results (subtask N may reference N-1)
                interpolated_args = self._interpolate_args(st.get("args", {}), results, plan=plan)
                st_to_dispatch = {**st, "args": interpolated_args}
                rpc_result = await self._dispatch_to_tool(st_to_dispatch)
                results[i] = rpc_result.result

        self._write_receipt(plan, results, src_evt)

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
    log.info("cortex.listening", host=host, port=port)
    async with websockets.serve(server.handle_glass, host, port):
        await asyncio.Future()  # run forever
