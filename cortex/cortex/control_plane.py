"""In-memory state mirror for the management console (Phase 3a.1).

Holds ring buffers for events, router LLM calls, tool dispatches, plus a
pub-sub for SSE live trace. Sits beside CortexServer; both populate it
(server writes events/dispatches, llm_cache observer writes LLM calls).

Memory budget by design: ~5 MB worst-case (rings cap each entry's JSON size).
Wipes on daemon restart — durable state remains in Twin (receipts/CHANGELOG).
Console must treat all rings as "recent window", not history of record.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import structlog

log = structlog.get_logger(__name__)


# ── ring sizes (tune via CLI later if needed) ──────────────────────────────

EVENT_RING_SIZE = 200       # ~200 events ≈ ~80 KB
LLM_RING_SIZE = 100         # ~100 LLM calls ≈ ~3 MB (prompt content dominates)
DISPATCH_RING_SIZE = 500    # ~500 RPC dispatches ≈ ~300 KB
LLM_PROMPT_MAX_CHARS = 25_000   # truncate per-message if absurdly large


# ── record shapes ───────────────────────────────────────────────────────────

TraceKind = Literal["event", "llm_call", "dispatch", "command", "receipt"]


@dataclass
class EventRecord:
    event_id: str
    kind: str
    ts: str           # ISO
    payload_brief: dict[str, Any]   # truncated; full text/image refs removed
    source: str       # "glass" | "tool_reverse_wake"


@dataclass
class LLMCallRecord:
    call_id: str               # monotonic local id
    ts: str
    purpose: str               # "router" | "twin_query" | ...
    model: str
    provider: str
    cache_hit: bool
    latency_ms: int
    prompt_chars: int
    completion_chars: int
    messages: list[dict[str, Any]] | None    # full prompts (truncated per-message)
    response: str | None                      # raw response text
    cache_key: str                            # first 10 chars
    error: str | None = None
    event_id: str | None = None               # binding back to the event that triggered this call
    attempt: int = 1


@dataclass
class DispatchRecord:
    rpc_id: str
    ts: str
    tool: str
    action: str
    args_brief: dict[str, Any]
    result_format: str
    status: str                # "ok" | "error" | "pending"
    result_brief: dict[str, Any] | None
    latency_ms: int | None
    event_id: str | None = None
    cmd_id: str | None = None


# ── helpers ────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _truncate(s: Any, n: int = 200) -> Any:
    if not isinstance(s, str):
        return s
    return s if len(s) <= n else (s[:n] + f"…<+{len(s) - n}>")


def _brief_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip large fields (image bytes) and truncate text."""
    if not isinstance(payload, dict):
        return {"_raw": _truncate(json.dumps(payload, ensure_ascii=False), 400)}
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k == "image":
            out[k] = "<image>" if v else None
        elif isinstance(v, str):
            out[k] = _truncate(v, 400)
        elif isinstance(v, dict):
            out[k] = _truncate(json.dumps(v, ensure_ascii=False), 400)
        else:
            out[k] = v
    return out


def _truncate_messages(msgs: list[dict[str, Any]], per_msg: int = LLM_PROMPT_MAX_CHARS) -> list[dict[str, Any]]:
    """Truncate per-message content to keep ring memory bounded."""
    out = []
    for m in msgs:
        c = m.get("content", "")
        if isinstance(c, str) and len(c) > per_msg:
            c = c[:per_msg] + f"…<+{len(c) - per_msg}>"
        out.append({"role": m.get("role"), "content": c})
    return out


# ── pub-sub subscriber ─────────────────────────────────────────────────────

@dataclass
class Subscriber:
    queue: asyncio.Queue[dict[str, Any]]
    kinds: set[TraceKind] | None = None   # None = all kinds


# ── main control plane ────────────────────────────────────────────────────

class ControlPlane:
    """Singleton-style state mirror. CortexServer + llm_telemetry write; HTTP reads."""

    def __init__(self) -> None:
        self._events: deque[EventRecord] = deque(maxlen=EVENT_RING_SIZE)
        self._llm_calls: deque[LLMCallRecord] = deque(maxlen=LLM_RING_SIZE)
        self._dispatches: deque[DispatchRecord] = deque(maxlen=DISPATCH_RING_SIZE)
        self._subscribers: list[Subscriber] = []
        self._llm_call_counter = 0
        self._totals = {
            "events_total": 0,
            "llm_calls_total": 0,
            "llm_cache_hits_total": 0,
            "llm_prompt_chars_total": 0,
            "llm_completion_chars_total": 0,
            "dispatches_total": 0,
            "dispatch_errors_total": 0,
        }
        # Bound when CortexServer is constructed; HTTP layer uses these for live reads
        self.server: Any = None  # CortexServer
        self.twin: Any = None    # Twin

    def bind(self, server: Any, twin: Any) -> None:
        self.server = server
        self.twin = twin

    # ── write paths (called from server.py + llm_telemetry observer) ──

    def record_event(self, event_id: str, kind: str, payload: dict[str, Any], source: str = "glass") -> None:
        rec = EventRecord(
            event_id=event_id, kind=kind, ts=_now_iso(),
            payload_brief=_brief_payload(payload), source=source,
        )
        self._events.append(rec)
        self._totals["events_total"] += 1
        self._broadcast("event", rec.__dict__)

    def record_llm_call(self, info: dict[str, Any]) -> None:
        self._llm_call_counter += 1
        msgs = info.get("messages")
        rec = LLMCallRecord(
            call_id=f"llm_{self._llm_call_counter:08d}",
            ts=_now_iso(),
            purpose=info.get("purpose", "?"),
            model=info.get("model", "?"),
            provider=info.get("provider", "openai"),
            cache_hit=bool(info.get("cache_hit", False)),
            latency_ms=int(info.get("latency_ms", 0)),
            prompt_chars=int(info.get("prompt_chars", 0)),
            completion_chars=int(info.get("completion_chars", 0)),
            messages=_truncate_messages(msgs) if isinstance(msgs, list) else None,
            response=_truncate(info.get("response"), LLM_PROMPT_MAX_CHARS) if info.get("response") else None,
            cache_key=str(info.get("cache_key", ""))[:10],
            error=info.get("error"),
            event_id=info.get("event_id"),
            attempt=int(info.get("attempt", 1)),
        )
        self._llm_calls.append(rec)
        self._totals["llm_calls_total"] += 1
        if rec.cache_hit:
            self._totals["llm_cache_hits_total"] += 1
        self._totals["llm_prompt_chars_total"] += rec.prompt_chars
        self._totals["llm_completion_chars_total"] += rec.completion_chars
        self._broadcast("llm_call", rec.__dict__)

    def record_dispatch_start(
        self, rpc_id: str, tool: str, action: str, args: dict[str, Any],
        result_format: str, event_id: str | None = None, cmd_id: str | None = None,
    ) -> None:
        rec = DispatchRecord(
            rpc_id=rpc_id, ts=_now_iso(),
            tool=tool, action=action,
            args_brief=_brief_payload(args),
            result_format=result_format,
            status="pending", result_brief=None, latency_ms=None,
            event_id=event_id, cmd_id=cmd_id,
        )
        self._dispatches.append(rec)
        self._totals["dispatches_total"] += 1
        self._broadcast("dispatch", rec.__dict__)

    def record_dispatch_end(
        self, rpc_id: str, status: str, result: dict[str, Any] | None, latency_ms: int,
    ) -> None:
        # Find the matching pending record (most recent wins) and mutate
        for rec in reversed(self._dispatches):
            if rec.rpc_id == rpc_id:
                rec.status = status
                rec.result_brief = _brief_payload(result or {})
                rec.latency_ms = latency_ms
                if status in ("failure", "tool_paused"):
                    self._totals["dispatch_errors_total"] += 1
                self._broadcast("dispatch", rec.__dict__)
                return

    # ── read paths (used by http.py) ──

    def list_events(self, limit: int = 50, since_ts: str | None = None) -> list[dict[str, Any]]:
        items = list(self._events)
        if since_ts:
            items = [e for e in items if e.ts > since_ts]
        return [dict(e.__dict__) for e in items[-limit:]]

    def list_llm_calls(self, limit: int = 50, since_ts: str | None = None) -> list[dict[str, Any]]:
        items = list(self._llm_calls)
        if since_ts:
            items = [c for c in items if c.ts > since_ts]
        return [dict(c.__dict__) for c in items[-limit:]]

    def get_llm_call(self, call_id: str) -> dict[str, Any] | None:
        for c in self._llm_calls:
            if c.call_id == call_id:
                return dict(c.__dict__)
        return None

    def list_dispatches(self, limit: int = 100, since_ts: str | None = None,
                        tool: str | None = None) -> list[dict[str, Any]]:
        items = list(self._dispatches)
        if since_ts:
            items = [d for d in items if d.ts > since_ts]
        if tool:
            items = [d for d in items if d.tool == tool]
        return [dict(d.__dict__) for d in items[-limit:]]

    def stats(self) -> dict[str, Any]:
        return {
            **self._totals,
            "events_in_ring": len(self._events),
            "llm_calls_in_ring": len(self._llm_calls),
            "dispatches_in_ring": len(self._dispatches),
            "subscribers": len(self._subscribers),
        }

    # ── SSE pub-sub ──

    def subscribe(self, kinds: set[TraceKind] | None = None) -> Subscriber:
        sub = Subscriber(queue=asyncio.Queue(maxsize=200), kinds=kinds)
        self._subscribers.append(sub)
        log.info("control_plane.subscribed", n_subs=len(self._subscribers), kinds=list(kinds) if kinds else "all")
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        try:
            self._subscribers.remove(sub)
            log.info("control_plane.unsubscribed", n_subs=len(self._subscribers))
        except ValueError:
            pass

    def _broadcast(self, kind: TraceKind, data: dict[str, Any]) -> None:
        if not self._subscribers:
            return
        msg = {"kind": kind, "data": data}
        for sub in list(self._subscribers):
            if sub.kinds is not None and kind not in sub.kinds:
                continue
            try:
                sub.queue.put_nowait(msg)
            except asyncio.QueueFull:
                # Slow consumer; drop oldest then push
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(msg)
                except Exception:
                    pass


# ── module-level singleton (constructed in main.py, used everywhere) ──

_PLANE: ControlPlane | None = None


def get_plane() -> ControlPlane:
    global _PLANE
    if _PLANE is None:
        _PLANE = ControlPlane()
    return _PLANE
