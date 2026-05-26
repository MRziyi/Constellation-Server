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

        lang_hint = (p.get("lang_hint") or "auto").lower()
        intent = (p.get("intent") or "fresh").lower()
        cmd_id = p.get("cmd_id")
        session_id = p.get("session_id")

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
        await self._emit_glass_frame("card", {
            "cmd_id": cmd_id,
            "title_runs": to_runs(title),
            "body_runs": to_runs(body_md),
            "scroll_total_lines": scroll_total_lines,
            "options": options or ["approve", "modify", "kill"],
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

    async def _send_command(self, cmd: Command) -> None:
        """Unified path: send the existing Command to the Glass peer AND,
        if the peer accepted glass-shaped frames, also emit the styled-runs
        flavor (card/hud_show → card or insight) + mic_open on CARD entry.

        Use this instead of `self._glass_conn.send(cmd.model_dump_json())`
        directly so glass-shaped frames stay in sync with the legacy ones."""
        if not self._glass_conn:
            return
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
            # Mic auto-opens on CARD entry only when the user has an actionable
            # decision to voice (not on empty-option info cards).
            if options:
                await self.emit_mic_open(stream_id=f"modify_{cmd.id}", ttl_ms=30_000)
        # hud_show → if the peer wants `insight` AND the payload carries
        # the insight marker, emit a glass `insight`. Otherwise the legacy
        # frame above is enough.
        elif cmd.kind == "hud_show":
            insight_kind = cmd.payload.get("_insight_kind")
            if insight_kind and "insight" in self._glass_accept:
                await self.emit_insight(
                    title=cmd.payload.get("title", ""),
                    body_md=cmd.payload.get("body", ""),
                    insight_kind=insight_kind,
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
    ) -> None:
        """Stash the (tmux, cc_session_id) so the next invoke in the same
        HUD session can reuse it via agent_continue. Caller must have
        dispatched the agent with keep_alive_on_final=True."""
        if not session_id:
            return
        tmux = agent_result.get("tmux_session")
        cc_sid = agent_result.get("session_id")
        if not tmux or not cc_sid:
            return
        self._active_hud_session_tmux[session_id] = {
            "tmux_session": tmux,
            "cc_session_id": cc_sid,
            "working_dir": working_dir,
            "timeout_s": float(timeout_s),
            "last_activity": time.time(),
            "last_summary": (
                ((agent_result.get("structured") or {}).get("summary") or "")[:120]
                if isinstance(agent_result.get("structured"), dict) else ""
            ),
        }
        log.info(
            "hud_tmux.registered",
            session_id=session_id, tmux=tmux, cc_sid=cc_sid[:8],
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
        """Return the fresh entry, or None if absent / stale."""
        if not session_id:
            return None
        entry = self._active_hud_session_tmux.get(session_id)
        if not entry:
            return None
        if (time.time() - float(entry.get("last_activity") or 0)) > self._HUD_TMUX_TTL_S:
            log.info(
                "hud_tmux.stale_skipping",
                session_id=session_id, tmux=entry.get("tmux_session"),
            )
            return None
        return entry

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
                for sid, entry in list(self._active_hud_session_tmux.items()):
                    age = now - float(entry.get("last_activity") or 0)
                    if age > self._HUD_TMUX_TTL_S:
                        stale.append(sid)
                if stale:
                    log.info("hud_tmux.sweep", n_stale=len(stale))
                for sid in stale:
                    await self._hud_tmux_evict(sid, reason="ttl_sweeper")
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
            body_md = (rpc_result.get("result_text") or "(no actions proposed)")[:1500]
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
                    "detail": "Modify clicked — please tell me how to change it.",
                }, ensure_ascii=False))
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
