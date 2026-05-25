"""Pydantic models for events / commands / RPC per INTERFACE-CONTRACTS.md."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Events (Glass / Tool / self → Cortex) per IC §1 ─────────────────────────


class UserInvokePayload(BaseModel):
    image: str | None = None  # base64 or blob ref (None when user_invoke has no image)
    text: str = ""  # empty string when user didn't speak


class UserDecisionPayload(BaseModel):
    in_reply_to: str  # cmd_*
    decision: Literal["send", "feedback", "dismiss"]
    feedback_text: str | None = None  # only present when decision == "feedback"


class ToolReverseWakePayload(BaseModel):
    from_tool: str
    wake_kind: Literal["permission_request", "completion_notice", "error", "surprising_event"]
    context: str
    options: list[dict[str, str]] = Field(default_factory=list)


class Event(BaseModel):
    """An event flowing into Cortex's Event Bus. Source-end may omit `id` —
    Cortex assigns one on ingress."""

    id: str | None = None
    ts: datetime
    kind: Literal[
        "user_invoke", "user_decision", "tool_reverse_wake", "self_pulse",
        # v2 agent runtime (per AGENT-ARCHITECTURE-V2.md):
        "agent_progress",      # tool_agent → Cortex: CC mid-task event (non-blocking)
        "progress_feedback",   # Glass → Cortex: user spoke in the feedback window
    ]
    payload: dict[str, Any]


# ── Commands (Cortex → Glass) per IC §2 ─────────────────────────────────────


class Command(BaseModel):
    id: str
    ts: datetime
    kind: Literal["hud_show", "preview_action", "tool_card", "hud_update", "hud_dismiss"]
    payload: dict[str, Any]
    requires_confirm: bool = False
    ttl_ms: int = 30_000


# ── RPC (Cortex ↔ Tool Agent) per IC §3 ─────────────────────────────────────


class RPCDispatch(BaseModel):
    id: str
    ts: datetime
    tool: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    context_pack: list[str] = Field(default_factory=list)
    result_format: Literal["draft", "execute", "query"]


class RPCResult(BaseModel):
    id: str  # matches the dispatch
    ts: datetime
    status: Literal["success", "failure", "needs_confirm", "tool_paused"]
    result: dict[str, Any] = Field(default_factory=dict)
    diagnostics: str | None = None
