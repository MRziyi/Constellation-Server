"""In-process Claude Agent SDK runner — P1 single-source path.

Replaces the tmux + jsonl-tail + pane-watch *dual-worker* observation of Claude
Code with the Agent SDK's **single ordered stream** (`query()` +
`include_partial_messages`) and its **native permission gate** (`can_use_tool`).
The two out-of-band workers were the root cause of the progress/permission
ordering hazard (a permission card could surface before the reasoning that
logically preceded it). One ordered stream removes the hazard at the source.

This module is ADDITIVE and gated by `USE_SDK_AGENT` (see `server._use_sdk_agent`).
When the flag is off, none of this runs and the tmux adapter path is unchanged —
it remains the default and the fallback.

Decision: Claude Code runs **in-process in Cortex** (Q1, Zack 2026-05-30). The
`can_use_tool` callback directly awaits a future that `_handle_user_decision`
resolves when the wearer's ring decision arrives over the glass WSS — no WSS hop
for permissions. This is safe with the existing deadlock fix: `_dispatch_complex_agent`
already runs as a backgrounded task (`_spawn_bg`), so parking it on the future
does not block the glass receive loop that delivers the decision.

Billing safety (Zack 2026-05-30, "能避免就避免"):
  - The spawned `claude` authenticates via the *subscription* — NOT a paid API key
    — as long as no `ANTHROPIC_API_KEY` is in the process env. We assert/warn on
    that at runtime (`_warn_if_api_key`). Auth is the macOS Keychain `/login`
    OAuth (or `CLAUDE_CODE_OAUTH_TOKEN` if the operator set one).
  - From 2026-06-15, Agent SDK / `claude -p` usage draws from a separate monthly
    "Agent SDK credit". When it runs out: with usage-credits disabled in the
    Console (the chosen config) requests STOP — they do not silently bill API.
    Exhaustion surfaces in-stream as `RateLimitEvent(status='rejected')` or
    `AssistantMessage.error in {'rate_limit','billing_error',...}`; we turn that
    into a visible card instead of a silent stall.
  - `max_budget_usd` is a second, per-run hard ceiling (env CORTEX_SDK_MAX_BUDGET_USD).

The runner returns an `rpc_result`-shaped dict IDENTICAL to what the tmux
`claude_code.agent` RPC returned ({ok, session_id, structured, result_text,
is_checkpoint, n_tool_uses, terminate_reason}). So everything downstream
(`_send_agent_card_for_decision`, modify-on-final, kill) works unchanged.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog

from . import ids

log = structlog.get_logger("cortex.sdk_agent")

# Per-run hard cost ceiling (belt; usage-credits-off is the real guarantee).
_MAX_BUDGET_USD = float(os.environ.get("CORTEX_SDK_MAX_BUDGET_USD", "2.0"))

# ── progress distillation (port of the tmux worker's _tool_glance et al.) ──
# White-box rule (Zack 2026-05-30, point 4): EVERY step says concretely what
# Claude is doing — reading X / editing Y / running Z / its narration text —
# and "thinking" appears ONLY for a real ThinkingBlock, never for plain text.

def _shorten(s: str, n: int = 70) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _basename_path(p: str) -> str:
    """Filename or last 2 path components — HUD friendly."""
    if not p:
        return "?"
    parts = p.split("/")
    return p if len(parts) <= 2 else ".../" + "/".join(parts[-2:])


def _mcp_pretty(tool: str) -> str:
    """`mcp__<server>__<action>` → glanceable verb (drop the noisy server prefix)."""
    seg = tool.split("__")[-1] if "__" in tool else tool
    return seg.replace("_", " ").strip() or tool


def _tool_glance(tool: str, inp: dict[str, Any]) -> tuple[str, str]:
    """(emoji, ≤70-char glanceable label) for one tool invocation. Exhaustive
    over classic + harness tool names so the wearer never sees a raw
    `ToolName(arg=…)` for a common action. Ported from the tmux adapter."""
    inp = inp or {}
    fp = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or "?"

    if tool in ("Bash", "PowerShell"):
        desc = inp.get("description") or ""
        if desc:
            return "🔧", _shorten(desc, 70)
        cmd = inp.get("command", "")
        first = (cmd.split() or ["?"])[0]
        return "🔧", _shorten(f"{first}: {cmd}", 70)
    if tool == "BashOutput":
        return "🔧", "reading shell output"
    if tool in ("KillShell", "KillBash"):
        return "🛑", "stopping background shell"
    if tool == "Monitor":
        return "👀", _shorten(f"monitoring: {inp.get('command') or inp.get('description') or '?'}", 70)
    if tool == "Read":
        rng = ""
        if inp.get("offset") or inp.get("limit"):
            a = inp.get("offset") or 0
            b = (a or 0) + (inp.get("limit") or 0)
            rng = f" (lines {a}-{b})" if b else ""
        return "📖", "reading " + _basename_path(fp) + rng
    if tool == "Write":
        return "✍️", "writing " + _basename_path(fp)
    if tool in ("Edit", "MultiEdit"):
        return "📝", "editing " + _basename_path(fp)
    if tool == "NotebookEdit":
        return "📓", _shorten(f"editing notebook {_basename_path(fp)}", 70)
    if tool == "LS":
        return "📂", "listing " + _basename_path(inp.get("path") or ".")
    if tool == "Glob":
        return "🔍", _shorten(f"glob {inp.get('pattern','?')}", 70)
    if tool == "Grep":
        return "🔍", _shorten(f"grep '{inp.get('pattern') or inp.get('query') or '?'}'", 70)
    if tool == "ToolSearch":
        return "🔍", _shorten(f"finding tools: {inp.get('query','?')}", 70)
    if tool == "WebFetch":
        return "🌐", _shorten(f"fetching {inp.get('url') or '?'}", 70)
    if tool == "WebSearch":
        return "🌐", _shorten(f"searching: {inp.get('query') or '?'}", 70)
    if tool in ("TodoWrite", "TodoRead"):
        return "📋", "updating todo list" if tool == "TodoWrite" else "checking todos"
    if tool in ("Task", "Agent"):
        sub = inp.get("subagent_type") or inp.get("agent_type") or "agent"
        return "🧵", _shorten(f"sub-agent: {sub}", 70)
    if tool == "Workflow":
        return "🧵", _shorten(f"workflow: {inp.get('name') or inp.get('title') or '?'}", 70)
    if tool == "EnterPlanMode":
        return "📋", "entering plan mode"
    if tool == "ExitPlanMode":
        return "✔️", "presenting a plan"
    if tool == "Skill":
        return "🎯", _shorten(f"skill: {inp.get('skill') or inp.get('command') or '?'}", 70)
    if tool == "SlashCommand":
        return "⌘", _shorten(f"/{(inp.get('command') or '?').lstrip('/')}", 70)
    if tool == "AskUserQuestion":
        qs = inp.get("questions")
        q = (qs[0].get("question") if isinstance(qs, list) and qs and isinstance(qs[0], dict)
             else inp.get("question")) or "?"
        return "❓", _shorten(f"asking: {q}", 70)
    if tool == "StructuredOutput":
        return "📦", "finalising result"
    if tool.startswith("mcp__") or "__" in tool or ":" in tool:
        return "🔌", _shorten(_mcp_pretty(tool), 70)
    if inp:
        k, v = next(iter(inp.items()))
        return "·", _shorten(f"{tool}({k}={v})", 70)
    return "·", _shorten(tool, 70)


def _edit_line_delta(tool: str, inp: dict[str, Any]) -> str | None:
    """For Edit/Write/MultiEdit, the net lines added/removed — the "Added 5 lines"
    / "Removed 3 lines" outcome line Zack wants under an edit."""
    def _lines(s: str) -> int:
        return (s.count("\n") + 1) if s else 0
    net = 0
    if tool == "Write":
        net = _lines(inp.get("content") or "")
    elif tool == "Edit":
        net = _lines(inp.get("new_string") or "") - _lines(inp.get("old_string") or "")
    elif tool == "MultiEdit":
        for e in (inp.get("edits") or []):
            if isinstance(e, dict):
                net += _lines(e.get("new_string") or "") - _lines(e.get("old_string") or "")
    else:
        return None
    if net > 0:
        return f"Added {net} line{'s' if net != 1 else ''}"
    if net < 0:
        return f"Removed {-net} line{'s' if -net != 1 else ''}"
    return "edited"


def _summarise_tool_result(content: str, is_err: bool) -> str:
    """Compress a tool result to a glanceable line (Bash output is the variable
    one). Ported from the tmux adapter."""
    if is_err:
        return _shorten(content.replace("\n", " ⏎ "), 70)
    text = (content or "").strip()
    if not text:
        return "(no output)"
    lines = text.split("\n")
    if len(lines) == 1:
        return _shorten(lines[0], 70)
    if len(lines) <= 3:
        return _shorten(" ⏎ ".join(lines), 70)
    return _shorten(f"{len(lines)} lines · 1st: {lines[0]}", 70)


def _flatten_tool_result(content: Any) -> str:
    """ToolResultBlock.content may be a str or a list of {type,text} blocks."""
    if isinstance(content, list):
        parts = []
        for x in content:
            parts.append(str(x.get("text", x)) if isinstance(x, dict) else str(x))
        return " ".join(parts)
    return str(content or "")


def _warn_if_api_key() -> None:
    """Billing guard: loudly warn if an API key is present in the env — that
    would make the headless `claude` bill at API rates instead of the
    subscription (ANTHROPIC_API_KEY is "always used when present" in -p mode)."""
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(k):
            log.warning(
                "sdk_agent.api_key_present",
                key=k,
                msg=("an API key is set — headless claude will bill at API rates, "
                     "NOT the subscription. unset it to use the Pro/Max plan."),
            )


class SdkAgentSession:
    """One complex-task agent run via the Agent SDK single ordered stream."""

    def __init__(
        self,
        server: Any,
        event: Any,
        *,
        brief: str,
        schema_hint: dict[str, Any] | None,
        add_dirs: list[str] | None,
        working_dir: str | None,
        permission_mode: str,
        timeout_s: float,
        resume_session_id: str | None = None,
    ) -> None:
        self.server = server
        self.event = event
        self.brief = brief
        self.schema_hint = schema_hint
        self.add_dirs = add_dirs or []
        self.working_dir = working_dir or os.path.expanduser("~")
        self.permission_mode = permission_mode
        self.timeout_s = timeout_s
        # Phase 2/3: when set, continue a prior CC session (modify-on-final +
        # UC2 archive resume) instead of starting fresh — full history restored.
        self.resume_session_id = resume_session_id
        self.session_id: str | None = resume_session_id
        self.n_tool_uses = 0
        self._client: Any = None  # set in run(); used by interrupt()=kill
        # tool_use_id → (name, input): lets a tool RESULT be labelled with the
        # right outcome (edit line counts) and dedupes repeated tool_use frames.
        self._tool_calls: dict[str, tuple[str, dict[str, Any]]] = {}
        self._emitted_tool_ids: set[str] = set()

    # ── permission gate (replaces pane-watch + send_keys) ──────────────────

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], ctx: Any):
        """Native permission callback. Builds a checkpoint/question card via the
        existing outbound queue (which parks the sender until decided — the
        card can't be buried), then awaits the wearer's ring decision and maps
        it to an SDK permission result.

          permission card  → approve=Allow · modify=Deny(reason=spoken correction,
                              flow continues) · reject=Deny(interrupt → stop flow,
                              session stays resumable)
          question card     → answer-only → Allow(updated_input carries the answer)
        """
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        is_question = tool_name == "AskUserQuestion"
        cmd_id = ids.command_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.server._sdk_pending[cmd_id] = {
            "fut": fut,
            "kind": "question" if is_question else "permission",
            "event_id": self.event.id,
        }

        # Human-readable card content straight from the SDK context — no pane
        # scraping. ToolPermissionContext carries title/display_name/description.
        if is_question:
            qs = (tool_input or {}).get("questions") or []
            first_q = (qs[0].get("question") if qs and isinstance(qs[0], dict) else "") or "Claude has a question."
            opts_lines = []
            for q in qs:
                for o in (q.get("options") or []):
                    lbl = o.get("label") if isinstance(o, dict) else str(o)
                    if lbl:
                        opts_lines.append(f"• {lbl}")
            body = first_q + (("\n\n" + "\n".join(opts_lines)) if opts_lines else "")
            await self.server.emit_card(
                cmd_id=cmd_id, title="Claude Needs You", body_md=body[:1500],
                options=["answer"], card_type="question", ttl_ms=600_000,
            )
        else:
            display = getattr(ctx, "display_name", None) or getattr(ctx, "title", None) or tool_name
            desc = getattr(ctx, "description", None) or _tool_glance(tool_name, tool_input)[1]
            body = f"**{display}**\n\n{desc}"
            await self.server.emit_card(
                cmd_id=cmd_id, title="Claude Needs You", body_md=body[:1500],
                options=["approve", "modify", "reject"], card_type="checkpoint",
                ttl_ms=600_000,
            )

        log.info("sdk_agent.permission_card", cmd_id=cmd_id, tool=tool_name,
                 kind="question" if is_question else "permission")

        # Park until the ring decision arrives (resolved by resolve_sdk_decision,
        # called from server._handle_user_decision). The dispatch task is
        # backgrounded so this await never blocks the glass receive loop.
        try:
            res = await fut
        except asyncio.CancelledError:
            self.server._sdk_pending.pop(cmd_id, None)
            raise

        action = (res.get("action") or "").strip().lower()
        fb = (res.get("feedback_text") or "").strip() or None
        log.info("sdk_agent.permission_resolved", cmd_id=cmd_id, action=action,
                 has_feedback=bool(fb))

        if is_question:
            # Answer-only: hand Claude the spoken reply. Best-effort shape for
            # AskUserQuestion — refine against a real question on first E2E.
            updated = dict(tool_input or {})
            updated["_answer"] = fb or ""
            return PermissionResultAllow(updated_input=updated)

        if action == "approve":
            return PermissionResultAllow()
        if action == "modify" and fb:
            # CC-native "tell Claude what to do instead": deny this action with
            # the correction as the reason; the flow continues (interrupt=False).
            return PermissionResultDeny(message=fb, interrupt=False)
        # reject / kill / anything else → stop this flow. The session_id persists
        # on disk, so it stays resumable later (reject→Esc semantics).
        return PermissionResultDeny(message="User rejected this action.", interrupt=True)

    # ── stream consumption ─────────────────────────────────────────────────

    async def _progress(self, stage: str, icon: str, detail: str) -> None:
        await self.server._emit_progress_to_glass(
            parent_event_id=self.event.id, stage=stage, icon=icon, detail=detail,
        )

    async def _handle_message(self, msg: Any) -> dict[str, Any] | None:
        """Translate one stream message → glass progress; return a terminal
        rpc_result dict when the stream ends, else None."""
        from claude_agent_sdk import (
            AssistantMessage, UserMessage, ResultMessage, RateLimitEvent,
            SystemMessage, TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
        )

        if isinstance(msg, SystemMessage):
            sid = (msg.data or {}).get("session_id") if isinstance(msg.data, dict) else None
            if sid:
                self.session_id = sid
            return None

        if isinstance(msg, RateLimitEvent):
            # status is the authoritative signal for THIS request:
            #   'allowed'         → fine, proceed
            #   'allowed_warning' → close to the limit, note it
            #   'rejected'        → the request was actually blocked → stopped
            # overage_status='rejected' is the NORMAL/desired state when usage
            # credits are disabled (paid overage off) — it is NOT exhaustion and
            # must NOT abort the run. (Verified 2026-05-30: a healthy run emits
            # status='allowed' overage_status='rejected'.)
            info = msg.rate_limit_info
            status = getattr(info, "status", None)
            if status == "rejected":
                return await self._credit_exhausted_card(info)
            if status == "allowed_warning":
                await self._progress("rate_warning", "⚠️",
                                     "Agent SDK 额度即将用尽")
            return None

        if isinstance(msg, AssistantMessage):
            self.session_id = msg.session_id or self.session_id
            err = getattr(msg, "error", None)
            if err in ("rate_limit", "billing_error"):
                return await self._credit_exhausted_card(None, reason=err)
            if err == "authentication_failed":
                return await self._error_card(
                    "认证失败", "Claude 认证失败——检查是否用订阅登录、且无 API key。")
            for block in (msg.content or []):
                if isinstance(block, ToolUseBlock):
                    # record for the matching tool_result; dedupe (partial +
                    # final envelopes can repeat the same tool_use id).
                    self._tool_calls[block.id] = (block.name, block.input or {})
                    if block.id in self._emitted_tool_ids:
                        continue
                    self._emitted_tool_ids.add(block.id)
                    self.n_tool_uses += 1
                    icon, label = _tool_glance(block.name, block.input or {})
                    await self._progress("tool_call", icon, label)
                elif isinstance(block, TextBlock):
                    # Claude's own narration / direct text output — NOT thinking.
                    # Show it verbatim (white-box). Only ThinkingBlock is ever
                    # labelled "thinking" (point 4, Zack 2026-05-30).
                    text = (block.text or "").strip()
                    if text:
                        await self._progress("assistant_text", "💬", text[:200])
                elif isinstance(block, ThinkingBlock):
                    think = (getattr(block, "thinking", "") or "").strip()
                    detail = ("thinking… " + think[:80]) if think else "thinking…"
                    await self._progress("thinking", "💭", detail)
            return None

        if isinstance(msg, UserMessage):
            # Tool results returning to Claude → show the OUTCOME (edit line
            # counts / command-output snippet / error). The other half of the
            # white-box stream (mirrors the tmux worker's tool_result frame).
            for block in (msg.content or []):
                if not isinstance(block, ToolResultBlock):
                    continue
                name, tin = self._tool_calls.get(
                    getattr(block, "tool_use_id", "") or "", (None, {}))
                is_err = bool(getattr(block, "is_error", False))
                if not is_err and name in ("Edit", "Write", "MultiEdit"):
                    await self._progress("tool_result", "✓",
                                         _edit_line_delta(name, tin) or "edited")
                else:
                    content = _flatten_tool_result(getattr(block, "content", ""))
                    await self._progress("tool_result", "✗" if is_err else "✓",
                                         _summarise_tool_result(content, is_err))
            return None

        if isinstance(msg, ResultMessage):
            self.session_id = msg.session_id or self.session_id
            if msg.api_error_status in (401, 403):
                return await self._error_card(
                    "认证/计费错误", f"Claude API error {msg.api_error_status}")
            structured = msg.structured_output if isinstance(msg.structured_output, dict) else None
            result_text = (msg.result or "").strip()
            if structured is None and result_text:
                structured = _parse_structured(result_text)
            log.info("sdk_agent.result", session=self.session_id,
                     is_error=msg.is_error, n_tool=self.n_tool_uses,
                     cost_usd=msg.total_cost_usd)
            return {
                "ok": not msg.is_error,
                "session_id": self.session_id,
                "structured": structured,
                "result_text": result_text,
                "is_checkpoint": _looks_checkpoint(structured),
                "n_tool_uses": self.n_tool_uses,
                "terminate_reason": msg.stop_reason or msg.subtype,
                "total_cost_usd": msg.total_cost_usd,
                "usage": msg.usage,
            }

        return None

    async def _credit_exhausted_card(self, info: Any, reason: str = "rate_limit") -> dict[str, Any]:
        resets = getattr(info, "resets_at", None) if info is not None else None
        when = f"（{resets} 刷新）" if resets else ""
        await self.server.emit_card(
            cmd_id=ids.command_id(), title="SDK 额度用尽",
            body_md=f"Claude Agent SDK 这个月的额度用尽了{when}。已停止，未产生 API 费用。",
            options=[], card_type="notification", ttl_ms=30_000,
        )
        log.warning("sdk_agent.credit_exhausted", reason=reason, resets_at=resets)
        return {"ok": False, "error": f"sdk_credit_{reason}", "session_id": self.session_id,
                "structured": None, "result_text": "", "is_checkpoint": False,
                "n_tool_uses": self.n_tool_uses}

    async def _error_card(self, title: str, body: str) -> dict[str, Any]:
        await self.server.emit_card(
            cmd_id=ids.command_id(), title=title, body_md=body,
            options=[], card_type="notification", ttl_ms=30_000,
        )
        return {"ok": False, "error": title, "session_id": self.session_id,
                "structured": None, "result_text": "", "is_checkpoint": False,
                "n_tool_uses": self.n_tool_uses}

    # ── entry point ────────────────────────────────────────────────────────

    async def run(self) -> dict[str, Any]:
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

        _warn_if_api_key()
        opts = ClaudeAgentOptions(
            permission_mode=self.permission_mode,   # 'acceptEdits' | 'bypassPermissions'
            can_use_tool=self._can_use_tool,
            include_partial_messages=True,          # single ordered stream w/ deltas
            cwd=self.working_dir,
            add_dirs=[str(d) for d in self.add_dirs],
            max_budget_usd=_MAX_BUDGET_USD,
            resume=self.resume_session_id,   # Phase 2/3: continue a prior session
        )
        await self._progress("dispatching_agent", "🤖",
                             f"dispatching agent ({self.permission_mode})")

        # Use ClaudeSDKClient, NOT a one-shot query(): the native permission
        # protocol (can_use_tool) needs the bidirectional stdio stream to stay
        # OPEN for the whole turn. With query() the input stream closes as soon
        # as the prompt is sent, so any permission request then fails with
        # "Stream closed" and can_use_tool never fires (verified on-box
        # 2026-05-30). The client also keeps the session alive for
        # interrupt()=kill and multi-turn modify (Phase 2); receive_response()
        # ends right after the ResultMessage, so there's no early-close race.
        client = ClaudeSDKClient(options=opts)
        self._client = client
        # Register so a kill decision can interrupt this in-flight turn.
        active = getattr(self.server, "_sdk_active", None)
        if active is not None:
            active[self.event.id] = self
        result: dict[str, Any] | None = None
        try:
            async def _drain() -> dict[str, Any] | None:
                await client.connect()
                await client.query(self.brief)
                async for msg in client.receive_response():
                    terminal = await self._handle_message(msg)
                    if terminal is not None:
                        return terminal
                return None

            result = await asyncio.wait_for(_drain(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            log.warning("sdk_agent.timeout", timeout_s=self.timeout_s)
            return {"ok": False, "error": "timeout", "session_id": self.session_id,
                    "structured": None, "result_text": "", "is_checkpoint": False,
                    "n_tool_uses": self.n_tool_uses}
        except Exception as e:
            log.error("sdk_agent.failed", error=str(e), exc_info=True)
            return {"ok": False, "error": str(e), "session_id": self.session_id,
                    "structured": None, "result_text": "", "is_checkpoint": False,
                    "n_tool_uses": self.n_tool_uses}
        finally:
            if active is not None:
                active.pop(self.event.id, None)
            self._client = None
            try:
                await client.disconnect()
            except Exception as e:
                log.debug("sdk_agent.disconnect_noisy", error=str(e))

        if result is None:
            # stream ended without a ResultMessage (unexpected)
            return {"ok": False, "error": "no_result", "session_id": self.session_id,
                    "structured": None, "result_text": "", "is_checkpoint": False,
                    "n_tool_uses": self.n_tool_uses}
        return result

    async def interrupt(self) -> None:
        """KILL the running turn (point 6: a flow ends as kill or run-to-end).
        Interrupts the in-flight SDK query; the session_id persists on disk so
        the killed flow stays resumable later (reject→Esc semantics)."""
        c = self._client
        if c is None:
            return
        try:
            await c.interrupt()
            log.info("sdk_agent.interrupted", session=self.session_id)
        except Exception as e:
            log.warning("sdk_agent.interrupt_failed", error=str(e))


def _parse_structured(text: str) -> dict[str, Any] | None:
    """Parse the agent's final JSON (CANONICAL_ACTIONS_SCHEMA) from its text,
    tolerant of fencing/prose — same intent as the tmux adapter's distiller."""
    if not text or "{" not in text:
        return None
    try:
        import json_repair
        obj = json_repair.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _looks_checkpoint(structured: Any) -> bool:
    """Mirror server._is_checkpoint without importing it (avoid coupling): a
    'next'/phase-style structured output is a checkpoint, not a final."""
    if not isinstance(structured, dict):
        return False
    return bool(structured.get("checkpoint")) or (
        "next" in structured and "actions" not in structured
    )


async def resolve_sdk_decision(
    server: Any, cmd_id: str, decision: str | None, feedback_text: str | None,
) -> bool:
    """Called early from server._handle_user_decision. If `cmd_id` is an
    outstanding SDK permission/question card, resolve its future (or open the
    mic for a modify/answer that still needs spoken text) and return True.
    Returns False if this cmd_id isn't an SDK card (caller falls through to the
    tmux / preview paths)."""
    pending = getattr(server, "_sdk_pending", None)
    if not pending:
        return False
    entry = pending.get(cmd_id)
    if not entry:
        return False

    d = (decision or "").strip().lower()
    fb = (feedback_text or "").strip()

    # modify / answer without text yet → open the mic, KEEP the card pending so
    # the can_use_tool await stays parked; the spoken correction returns via the
    # STT-review gate as a user_decision carrying feedback_text for this cmd_id.
    if d == "modify" and not fb:
        await server.emit_mic_open(stream_id=f"modify_{cmd_id}", ttl_ms=30_000)
        return True
    if d == "answer" and not fb:
        await server.emit_mic_open(stream_id=f"answer_{cmd_id}", ttl_ms=30_000)
        return True

    pending.pop(cmd_id, None)
    fut = entry.get("fut")
    if fut is not None and not fut.done():
        fut.set_result({"action": d, "feedback_text": fb or None})
    return True
