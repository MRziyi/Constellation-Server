"""Cortex intent classifier (Phase 5c).

Single-call decision for every user_invoke: should this ask go through the
agent path (Claude Code in tmux with the v2.6 brief + actions[] schema +
phase checkpoints) OR through the existing direct-adapter path (v0.5 selector
+ planner → a single executor adapter dispatch)?

Why a separate classifier instead of letting the v0.5 planner figure it out:
  - Planner runs Opus and burns ~$0.01 per call to "think" about a 6-word ask
    like "battery?". Classifier is haiku ≈ $0.0001 + sub-second.
  - The classification is genuinely a small task: it's an intent label, not
    a full plan. Haiku is enough.
  - It lets the agent path (which has its own brief assembly + selector +
    schema) be cleanly distinct from the simple-action path (which uses the
    existing 7 executor adapters via the v0.5 Router prompt).

The classifier prompt is intentionally tiny. Keep it that way — its job is
narrow: one bit + a 15-word reason for telemetry.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import structlog

from .llm_cache import cached_chat_create, parse_json_response
from .prompts import CLASSIFIER_MODEL, CLASSIFIER_SYSTEM
from .schema import Event

log = structlog.get_logger(__name__)


async def classify_intent(event: Event) -> dict[str, Any]:
    """Return {"complex": bool, "why": str, "raw": ..., "error"?: str}.

    Defensive: on any failure, defaults to complex=True (the agent path is
    capable of handling simple asks too, just slower; failing closed to
    complex avoids missing a research need).
    """
    payload = event.payload or {}
    text = (payload.get("text") or "").strip()
    if not text:
        # No text → nothing to classify. Default to simple (e.g. photo-only
        # asks should hit the existing path which can route to vision tools).
        return {"complex": False, "why": "no text provided", "raw": None}

    user_prompt = f'Ask: "{text}"'
    if payload.get("image"):
        user_prompt += "\nNote: photo attached."

    try:
        raw = await cached_chat_create(
            model=CLASSIFIER_MODEL,
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            purpose="classifier",
        )
        parsed = parse_json_response(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("complex"), bool):
            effort = str(parsed.get("effort", "")).strip().lower()
            if effort not in ("low", "medium", "high", "xhigh", "max"):
                effort = "low"   # default: most agent tasks are mechanical
            log.info(
                "classifier.decided",
                complex=parsed["complex"],
                why=str(parsed.get("why", ""))[:80],
                effort=effort,
                text_preview=text[:60],
            )
            return {
                "complex": parsed["complex"],
                "why": str(parsed.get("why", "")),
                "effort": effort,
                "raw": raw,
            }
        log.warning("classifier.invalid_shape", parsed=str(parsed)[:120])
        return {"complex": True, "why": "classifier returned invalid shape", "raw": raw}
    except Exception as e:
        log.warning("classifier.failed", error=str(e), error_type=type(e).__name__)
        return {"complex": True, "why": f"classifier error: {e}", "raw": None, "error": str(e)}
