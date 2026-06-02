"""Cortex voice-driven shortcut-slot config parser.

The Glass client exposes 3 fixed shortcut slots (shortcut1/2/3) to the Halo
Ring; their content is owned by the app and edited BY VOICE. When the wearer
says e.g. "把 shortcut2 改成发一张照片并问面前是什么东西" / "set shortcut 2 to
send a photo and ask what's in front", this module:

  1. detects the utterance is a slot-CONFIG command (not a normal query / not a
     fire), via `looks_shortcut_config`, and
  2. parses it into a structured slot update `{slot, prompt, send_photo, label}`
     via `parse_shortcut_config` (small LLM call, same model as the classifier).

The orchestrator (server.py `_handle_user_invoke`) emits the result as a
`shortcut_config` glass frame; the Glass applies it to its local slot store.

`send_photo` here is the slot's default: when true, the slot attaches a photo
UPFRONT at fire time (so the server's R-13 on-demand pull is skipped — no
duplicate capture).
"""

from __future__ import annotations

from typing import Any

import structlog

from .llm_cache import cached_chat_create, parse_json_response
from .prompts import (
    SHORTCUT_PARSER_MODEL as PARSER_MODEL,
    SHORTCUT_PARSER_SYSTEM as SYSTEM_PROMPT,
    SLOT_RE as _SLOT_RE,
    CONFIG_VERB_RE as _CONFIG_VERB_RE,
    VISION_DETAIL_PATTERN as _VISION_DETAIL_PATTERN,
)

log = structlog.get_logger(__name__)

N_SLOTS = 3

# Detection patterns (_SLOT_RE / _CONFIG_VERB_RE) now live in prompts.py
# (imported at the top): a slot-config utterance names a slot (shortcut N) AND
# has a config verb; a fire ("run shortcut 2") has no verb → falls through.


def detect_slot(text: str) -> int | None:
    """Return the slot number 1..3 named in the text, or None."""
    if not text:
        return None
    m = _SLOT_RE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= N_SLOTS else None


def looks_shortcut_config(text: str) -> bool:
    """True iff the utterance is configuring a shortcut slot (names a slot +
    has a config verb). A plain fire or a normal query returns False."""
    if not text:
        return False
    return detect_slot(text) is not None and bool(_CONFIG_VERB_RE.search(text))


async def parse_shortcut_config(text: str) -> dict[str, Any] | None:
    """Parse a slot-config utterance into {slot, prompt, send_photo, label}.
    Returns None on failure / invalid shape (caller falls back to the normal
    pipeline). `slot` is clamped to 1..3; a regex-detected slot overrides the
    LLM if they disagree (the regex is reliable for the number)."""
    regex_slot = detect_slot(text)
    try:
        raw = await cached_chat_create(
            model=PARSER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text.strip()},
            ],
            purpose="shortcut_config",
        )
        parsed = parse_json_response(raw)
        if not isinstance(parsed, dict):
            log.warning("shortcut_config.invalid_shape", parsed=str(parsed)[:120])
            return None
        slot = regex_slot or parsed.get("slot")
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return None
        if not (1 <= slot <= N_SLOTS):
            return None
        prompt = str(parsed.get("prompt") or "").strip()
        if not prompt:
            log.warning("shortcut_config.empty_prompt", slot=slot)
            return None
        send_photo = bool(parsed.get("send_photo", False))
        # People-Recall (Zack 2026-06-01): a slot can carry a face MODE.
        # face_recall = recognize who's in front; enroll_person = remember a new
        # person. Both always take a photo. Anything else → "task" (normal).
        mode = str(parsed.get("mode") or "task").strip()
        if mode not in ("task", "face_recall", "enroll_person"):
            mode = "task"
        if mode in ("face_recall", "enroll_person"):
            send_photo = True
        label = str(parsed.get("label") or "").strip()[:40] or prompt[:24]
        # Capture TIER (Zack 2026-05-31): when a photo-bearing slot fires, the
        # glasses capture at this tier. Deterministic — infer from the wording
        # (prompt + the config utterance), same VISION_DETAIL_PATTERN the live
        # vision path uses: a detail qualifier (细节 / detail / 高清 / 2k) → 'detail'
        # (2048px/q90, legible text); else 'standard' (1024px/q85, fast glance).
        tier = "detail" if _VISION_DETAIL_PATTERN.search(f"{prompt} {text}") else "standard"
        result = {
            "slot": slot,
            "prompt": prompt,
            "send_photo": send_photo,
            "label": label,
            "tier": tier,
            "mode": mode,
        }
        log.info("shortcut_config.parsed", **result)
        return result
    except Exception as e:
        log.warning(
            "shortcut_config.failed",
            error=str(e), error_type=type(e).__name__,
        )
        return None
