"""Cortex session router (R-14 / C-56).

Voice-addressable parallel sessions. Runs at the top of `_handle_user_invoke`
BEFORE the classifier. Inputs: (prompt, active_sessions_index) → decides
whether to continue an existing CC tmux session or create a new one.

Why a separate router (not part of classifier):
  - Classifier decides "complex vs simple" (path choice).
  - Router decides "which session does this prompt belong to" (target).
  - Both are sub-second LLM calls but they're orthogonal — running them
    together would mix concerns + complicate prompts.

When does this fire:
  - On every `user_invoke` IF >= 2 active sessions exist.
  - Skipped (short-circuited) when 0 or 1 active sessions:
      0 → caller mints a new session (existing behavior)
      1 → caller uses that single session (existing behavior)

The router is deliberately CONSERVATIVE — when uncertain, it prefers the
current session (event.payload.session_id) to avoid surprising the user.
Future R-14.c will add a confirmation CARD for the 0.4-0.7 confidence band.

Output schema:
  {
    "decision":           "continue" | "new",
    "target_session_id":  "ses_xxxx" | null,
    "confidence":         0.0-1.0,
    "why":                "≤15 words explaining the decision"
  }
"""

from __future__ import annotations

import os
import time
from typing import Any

import structlog

from .llm_cache import cached_chat_create, parse_json_response

log = structlog.get_logger(__name__)


# Same model as classifier (gpt-5.2 + diskcache by default). No new dependency.
ROUTER_MODEL = os.environ.get(
    "CORTEX_SESSION_ROUTER_MODEL",
    os.environ.get("CORTEX_CLASSIFIER_MODEL",
                   os.environ.get("CORTEX_ROUTER_MODEL", "gpt-5.2")),
)

# Confidence ≥ this → silently route. Below → for now, also route (R-14.a);
# in R-14.c we'll emit a confirmation CARD between 0.4 and this threshold.
HIGH_CONFIDENCE = 0.7


SYSTEM_PROMPT = """\
You're Cortex's session router. Single job: given a new user prompt and a list
of currently-active CC sessions, decide which session the prompt belongs to OR
whether to create a new one.

Output JSON ONLY: {"decision": "continue"|"new", "target_session_id": "ses_..."|null, "confidence": 0.0-1.0, "why": "≤15 words"}

Rules:
- If the prompt clearly references an existing session's topic (subject overlap,
  pronouns like "the email one"/"that auth refactor"/"那个邮件"/"那个 auth"),
  pick decision="continue" + that session_id. High confidence (0.8-1.0).
- If the prompt is a clearly new topic unrelated to all active sessions,
  pick decision="new" + target_session_id=null. High confidence.
- If the prompt is a short follow-up ("yes", "more", "next", "嗯", "继续") AND
  there's exactly one recently-active session (last activity < 5 min), continue
  that one with high confidence.
- If ambiguous (prompt could plausibly belong to multiple existing sessions or
  could equally be new), pick the most-recently-active matching session with
  medium confidence (0.5-0.7). The orchestrator may show a confirmation card.
- When in doubt, prefer "continue" the current session (the caller passes
  current_session_id in the input; weight it heavily).

JSON ONLY. No fence. No prose.
"""


def _build_user_prompt(
    text: str,
    active_sessions: list[dict[str, Any]],
    current_session_id: str | None,
    has_image: bool,
) -> str:
    """Render the router input. `active_sessions` items have keys: session_id,
    title, last_summary, last_activity (epoch s), turn_count, created_at."""
    lines: list[str] = [f'New prompt: "{text}"']
    if has_image:
        lines.append("(photo attached)")
    lines.append(f"Current session_id (in HUD context): {current_session_id or 'none'}")
    lines.append("")
    lines.append(f"Active sessions ({len(active_sessions)}, most recent first):")
    now = time.time()
    for s in active_sessions:
        age_min = int((now - float(s.get("last_activity") or now)) / 60.0)
        title = s.get("title") or "(untitled)"
        summary = (s.get("last_summary") or "")[:80]
        turns = s.get("turn_count", 0)
        lines.append(
            f'  • {s["session_id"]} — "{title}"  (turns={turns}, last={age_min}min)'
        )
        if summary:
            lines.append(f'      last_summary: "{summary}"')
    return "\n".join(lines)


async def route_session(
    *,
    text: str,
    active_sessions: list[dict[str, Any]],
    current_session_id: str | None,
    has_image: bool = False,
) -> dict[str, Any]:
    """Return routing decision. On any LLM failure, falls back to
    `decision="continue" / target=current_session_id` (safest: keeps user
    in their current thread).

    Caller should short-circuit BEFORE calling this when len(active_sessions) <= 1.
    """
    if not text or not text.strip():
        return {
            "decision": "continue",
            "target_session_id": current_session_id,
            "confidence": 1.0,
            "why": "empty text, no-op route",
        }

    user_prompt = _build_user_prompt(text.strip(), active_sessions, current_session_id, has_image)

    try:
        raw = await cached_chat_create(
            model=ROUTER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            purpose="session_router",
        )
        parsed = parse_json_response(raw)
        if not isinstance(parsed, dict) or parsed.get("decision") not in ("continue", "new"):
            log.warning("session_router.invalid_shape", parsed=str(parsed)[:120])
            return {
                "decision": "continue",
                "target_session_id": current_session_id,
                "confidence": 0.0,
                "why": "router invalid shape, defaulting to current",
            }
        decision = parsed["decision"]
        target = parsed.get("target_session_id") or None
        confidence = float(parsed.get("confidence", 0.0))
        why = str(parsed.get("why", ""))[:80]
        # Validate target_session_id is in the active set when decision=continue
        if decision == "continue":
            if not target or target not in {s["session_id"] for s in active_sessions}:
                log.warning(
                    "session_router.target_not_in_active",
                    target=target,
                    active=[s["session_id"] for s in active_sessions],
                )
                # Fall back to current
                target = current_session_id
        else:
            target = None  # "new" implies caller mints
        log.info(
            "session_router.decided",
            decision=decision, target=target,
            confidence=confidence, why=why,
            n_active=len(active_sessions),
            current=current_session_id,
            text_preview=text[:60],
        )
        return {
            "decision": decision,
            "target_session_id": target,
            "confidence": confidence,
            "why": why,
        }
    except Exception as e:
        log.warning(
            "session_router.failed",
            error=str(e), error_type=type(e).__name__,
        )
        return {
            "decision": "continue",
            "target_session_id": current_session_id,
            "confidence": 0.0,
            "why": f"router error: {e}",
        }
