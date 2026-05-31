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
import os
from typing import Any, Literal

import structlog

from .llm_cache import cached_chat_create, parse_json_response
from .schema import Event

log = structlog.get_logger(__name__)


# Classifier model: defaults to the same model Router uses (Cortex's OpenAI
# key is already configured for that). If/when an Anthropic API key gets
# added, switch the default to a haiku alias for cheaper + faster classification.
CLASSIFIER_MODEL = os.environ.get(
    "CORTEX_CLASSIFIER_MODEL",
    os.environ.get("CORTEX_ROUTER_MODEL", "gpt-5.2"),
)


CLASSIFIER_SYSTEM = """\
You're Cortex's intent classifier. Single job: route Zack's ask to either the
research-agent path (Claude — multi-step) or the direct-adapter path (one
bounded side-effect or state read).

Output JSON ONLY: {"complex": true|false, "why": "≤15 words"}

complex = true  WHEN the ask needs ANY of:
  - reading/finding/searching multiple sources (emails, files, sessions, web)
  - composition / drafting / summarising
  - multiple side-effect actions in one ask
  - keywords: find / look at / search / summarise / draft / check / 看 / 找 / 起草

complex = false WHEN it's a SINGLE explicit step:
  - bounded reminder: "remind me to X at 3pm" (title + time both given)
  - bounded calendar: "add a 4pm meeting with Y tomorrow"
  - bounded message: "send 'on my way' to Mike" (recipient + content both given)
  - pure state query: "battery?", "what time?", "focus mode?", "current tab?"
  - bounded file write: "write 'X' to /tmp/y.txt"

A photo may be attached ("Note: photo attached."). It does NOT change this
decision — classify on the TASK, not the photo: a bounded one-step ask is simple
even with a photo ("add a reminder for the time in this photo", "what is this");
a multi-step / compose / persist-to-twin ask is complex.

When ambiguous, prefer complex=true — the agent path can degrade to a single
action; the direct path can't escalate to research.

JSON ONLY. No fence. No prose.
"""


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
            log.info(
                "classifier.decided",
                complex=parsed["complex"],
                why=str(parsed.get("why", ""))[:80],
                text_preview=text[:60],
            )
            return {
                "complex": parsed["complex"],
                "why": str(parsed.get("why", "")),
                "raw": raw,
            }
        log.warning("classifier.invalid_shape", parsed=str(parsed)[:120])
        return {"complex": True, "why": "classifier returned invalid shape", "raw": raw}
    except Exception as e:
        log.warning("classifier.failed", error=str(e), error_type=type(e).__name__)
        return {"complex": True, "why": f"classifier error: {e}", "raw": None, "error": str(e)}
