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

import os
import re
from typing import Any

import structlog

from .llm_cache import cached_chat_create, parse_json_response

log = structlog.get_logger(__name__)

PARSER_MODEL = os.environ.get(
    "CORTEX_CLASSIFIER_MODEL",
    os.environ.get("CORTEX_ROUTER_MODEL", "gpt-5.2"),
)

N_SLOTS = 3

# Detection: utterance names a slot (shortcut N / 快捷方式 N) AND has a config
# verb. Anchored loosely — a fire ("run shortcut 2") has no config verb, so it
# falls through to the normal path.
_SLOT_RE = re.compile(r"(?:shortcut|快捷(?:方式|键)?)\s*([1-3])", re.IGNORECASE)
_CONFIG_VERB_RE = re.compile(
    r"(set|change|configure|update|edit|rename|make|定义|设为|设成|设置|"
    r"改成|改为|换成|配置|绑定|编辑)",
    re.IGNORECASE,
)


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


SYSTEM_PROMPT = """\
You configure one of the user's 3 voice shortcut slots. The user's utterance
edits a slot — extract what it should become. The slot, when later fired,
sends a PRESET PROMPT to the assistant (optionally with a photo attached).

Output JSON ONLY:
{"slot": 1|2|3, "prompt": "<the preset prompt to send when fired>", "send_photo": true|false, "label": "<≤4-word name>"}

Rules:
- `slot`: the slot number the user named (1, 2, or 3).
- `prompt`: the actual prompt the shortcut will SEND when fired — NOT the
  configuration command. E.g. for "set shortcut 2 to ask what's in front of
  me", prompt = "What's in front of me?" (not "set shortcut 2 …").
- `send_photo`: true if firing should attach a camera photo — set true when the
  user says to send/attach a photo, OR when the prompt inherently needs to see
  the scene (what's in front, read this, identify this). Else false.
- `label`: a short human name for the slot (≤4 words), derived from the prompt.
- Preserve the user's language for `prompt` + `label` (Chinese in → Chinese out).

JSON ONLY. No fence. No prose.
"""


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
        label = str(parsed.get("label") or "").strip()[:40] or prompt[:24]
        result = {
            "slot": slot,
            "prompt": prompt,
            "send_photo": send_photo,
            "label": label,
        }
        log.info("shortcut_config.parsed", **result)
        return result
    except Exception as e:
        log.warning(
            "shortcut_config.failed",
            error=str(e), error_type=type(e).__name__,
        )
        return None
