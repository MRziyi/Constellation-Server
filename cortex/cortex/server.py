"""WebSocket server — Glass-facing endpoint.

Phase 2 Slice A: real Router (when OPENAI_API_KEY present) or stub fallback.
Phase 3+: full Hybrid Connection Model (see INTERFACE-CONTRACTS.md §1.6) with push wake.
"""

from __future__ import annotations

import asyncio
import json
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
_STAGE_DEFAULT_ICONS: dict[str, str] = {
    "started":             "🤖",
    "brief_sent":          "▶️",
    "tool_call":           "🔧",
    "tool_result":         "✓",
    "assistant_text":      "💭",
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
        plan = await self._route(event)
        log.info("plan.generated", primary_intent=plan["primary_intent"])

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
        for st in plan["subtasks"]:
            if st["result_format"] in ("draft", "query") or hud_kind == "hud_show":
                # Re-interpolate args (subtask N may reference N-1's result)
                args = self._interpolate_args(st.get("args", {}), subtask_results, plan=plan)
                rpc_result = await self._dispatch_to_tool({**st, "args": args})
                subtask_results.append(rpc_result.result)
            else:
                subtask_results.append({})  # placeholder so indices align

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
        return Command(
            id=ids.command_id(),
            ts=datetime.now(timezone.utc),
            kind=hud["kind"],
            payload={
                "title": hud["title"],
                "body": body,
                "icon": hud.get("icon", ""),
                "options": hud.get("options", []),
            },
            requires_confirm=hud["kind"] == "preview_action",
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
        pending = self._pending_previews.pop(cmd_id, None)
        if not pending:
            log.warning("user_decision.no_pending", cmd_id=cmd_id, known=list(self._pending_previews.keys()))
            return

        # Reverse-wake follow-up: decision matches an option id in wake_response_map.
        wake_response_map = pending.get("wake_response_map") or {}
        if wake_response_map and decision in wake_response_map:
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

        if decision == "dismiss":
            log.info("dismissed")
            return

        plan = pending["plan"]
        task_continues = bool(plan.get("task_continues"))
        task_history: list[dict[str, Any]] = list(pending.get("task_history") or [])
        original_event: Event = pending["event"]

        if decision == "send":
            if task_continues:
                # Intermediate step: defensively run any execute subtasks (Router should
                # not emit those for intermediate, but allow + warn).
                exec_subtasks = [st for st in plan["subtasks"] if st["result_format"] == "execute"]
                if exec_subtasks:
                    log.warning("intermediate.has_execute_subtasks", n=len(exec_subtasks))
                    await self._execute_remaining_no_receipt(pending)
                # Write a step receipt (audit trail mid-task)
                self._write_step_receipt(plan, pending["subtask_results"], event.id, step_index=len(task_history))
                task_history.append(self._summarize_step_for_history(plan, pending["subtask_results"], "send"))
                try:
                    await self._advance_task(original_event, task_history, event.id)
                except Exception as e:
                    log.error("task.advance_failed", error=str(e), error_type=type(e).__name__, exc_info=True)
                    raise
            else:
                # Final step: execute + write final receipt
                try:
                    await self._execute_remaining(pending, event.id)
                except Exception as e:
                    log.error("execute_remaining.failed", error=str(e), error_type=type(e).__name__, exc_info=True)
                    raise

        elif decision == "feedback":
            # Free-form verbal response; ALWAYS re-route with feedback + task_history.
            # Router decides: redo current step / advance / skip / inject info (per C-23).
            task_history.append(self._summarize_step_for_history(
                plan, pending["subtask_results"], "feedback", feedback_text
            ))
            try:
                await self._advance_task(
                    original_event, task_history, event.id,
                    feedback_text=feedback_text,
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
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_rpcs[dispatch.id] = fut
        import time as _time
        t0 = _time.monotonic()
        try:
            await self._tool_conn.send(dispatch.model_dump_json())
            result = await asyncio.wait_for(fut, timeout=120.0)
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
                    result={"error": "timeout after 120s"},
                    latency_ms=int((_time.monotonic() - t0) * 1000),
                )
            raise RuntimeError(f"RPC {dispatch.id} timed out after 120 s")


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
