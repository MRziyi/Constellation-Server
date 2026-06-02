"""People-Recall: parse a spoken bio into a structured person record.

The enroll journey (Zack 2026-06-01): the wearer fires the *remember* shortcut
(captures the person's face), then speaks a freeform bio. After the STT-review
gate approves it, this module turns that transcript into a structured record —
ONE cheap LLM completion (same model as the classifier), not an agent loop. The
deterministic face embedding is computed separately (`face_index`); this only
does the prose→fields structuring + a dense `recall` blurb for the recall card.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import structlog

from .llm_cache import cached_chat_create, parse_json_response
from .prompts import (
    ENROLL_PARSER_MODEL as PARSER_MODEL,
    ENROLL_PARSER_SYSTEM as SYSTEM_PROMPT,
)

log = structlog.get_logger(__name__)


def slugify(name: str) -> str:
    """ASCII slug for a filename / index key. Falls back to a stable token when
    the name is non-latin (e.g. CJK) so we always get a usable path."""
    norm = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")
    if not slug:
        # Non-latin name → keep the alnum (incl. CJK) joined by hyphens.
        slug = re.sub(r"\s+", "-", re.sub(r"[^\w]+", " ", name).strip()).lower()
    return slug[:48] or "person"


async def parse_person(transcript: str) -> dict[str, Any] | None:
    """Parse a spoken bio into {name, slug, aliases[], org, role, met_at,
    research, recall, notes}. Returns None on failure / empty name (caller
    shows a 'couldn't catch that' card)."""
    text = (transcript or "").strip()
    if not text:
        return None
    try:
        raw = await cached_chat_create(
            model=PARSER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            purpose="enroll_parse",
        )
        parsed = parse_json_response(raw)
    except Exception as e:
        log.warning("enroll_parse.failed", error=str(e), error_type=type(e).__name__)
        return None
    if not isinstance(parsed, dict):
        log.warning("enroll_parse.invalid_shape", parsed=str(parsed)[:120])
        return None
    name = str(parsed.get("name") or "").strip()
    if not name:
        log.warning("enroll_parse.no_name")
        return None
    aliases = parsed.get("aliases")
    aliases = [str(a).strip() for a in aliases if str(a).strip()] if isinstance(aliases, list) else []
    rec = {
        "name": name,
        "slug": slugify(name),
        "aliases": aliases,
        "org": str(parsed.get("org") or "").strip(),
        "role": str(parsed.get("role") or "").strip(),
        "met_at": str(parsed.get("met_at") or "").strip(),
        "research": str(parsed.get("research") or "").strip(),
        "recall": str(parsed.get("recall") or "").strip()[:120],
        "notes": str(parsed.get("notes") or "").strip(),
    }
    if not rec["recall"]:
        # Always have something to show on the recall card.
        rec["recall"] = " · ".join(x for x in (rec["met_at"], rec["org"], rec["research"]) if x) or name
    log.info("enroll_parse.parsed", name=name, slug=rec["slug"], recall=rec["recall"])
    return rec
