"""Inbound-email → HUD push helpers (Zack 2026-06-05).

A VIP sender emails Zack → his Mac (a Mail.app rule, see
docs/server/MAIL-INBOUND-RULE.md) POSTs the message to cortex's
`/api/mail/inbound` → cortex pushes a HUD card showing the email. Zack
LONG-presses the card and speaks a reply instruction ("reply to him saying
…" / "look in my memo for the poster title and tell him"); that runs the
NORMAL voice pipeline (STT review → classifier → dispatch) with the email
body + Message-ID injected as context, so the agent knows exactly what it's
replying to and threads correctly (`reply_to_message_id`).

This module is the pure/testable part: VIP allowlist loading, the card body,
and the context block prepended to the spoken instruction. The wiring (card
emit, pending registry, mic re-open, dispatch) lives in server.py.

Design notes:
  - VIP-only is FAIL-CLOSED: a missing/empty allowlist triggers NOTHING, so a
    fresh install never spams the HUD with promos. Zack adds addresses to
    ~/constellation/twin/_system/mail_vips.txt (one email per line; # comments).
  - We never reduce the email to a "summary" — Zack sees the real body on the
    HUD (like the pre-send confirm page) and the agent gets the real body as
    context. No OCR / describe-to-text step (C-77 spirit).
"""

from __future__ import annotations

import os
import re
from typing import Any

# Default VIP allowlist file. One email address per line; blank lines and
# lines starting with '#' are ignored. Matching is case-insensitive on the
# bare address. Override with env CONSTELLATION_MAIL_VIPS.
DEFAULT_VIP_PATH = "~/constellation/twin/_system/mail_vips.txt"

# Cap the email body we carry — both onto the HUD card (it scrolls) and into
# the agent context. Long enough to read a real message, bounded so a giant
# newsletter doesn't blow the prompt.
_BODY_CARD_CAP = 1200
_BODY_CTX_CAP = 1500

_ADDR_RE = re.compile(r"<([^>]+)>")


def vip_path() -> str:
    return os.path.expanduser(
        os.environ.get("CONSTELLATION_MAIL_VIPS", DEFAULT_VIP_PATH))


def extract_addr(sender: str | None) -> str:
    """Pull the bare email address out of a 'Name <addr@x>' string (or return
    the trimmed token if it's already bare). Lower-cased for matching."""
    if not sender:
        return ""
    m = _ADDR_RE.search(sender)
    addr = m.group(1) if m else sender
    return addr.strip().strip("<>").lower()


def load_vip_senders(path: str | None = None) -> set[str]:
    """Read the VIP allowlist → set of lower-cased bare addresses. Missing or
    unreadable file → empty set (fail-closed: nothing triggers)."""
    p = path or vip_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return set()
    out: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(extract_addr(line))
    out.discard("")
    return out


def is_vip(sender_email: str | None, vips: set[str]) -> bool:
    """True iff the sender's bare address is on the allowlist."""
    return extract_addr(sender_email) in vips if vips else False


def _clip(s: str | None, cap: int) -> str:
    s = (s or "").strip()
    if len(s) <= cap:
        return s
    return s[:cap].rstrip() + " …(truncated)"


def card_body(ctx: dict[str, Any]) -> str:
    """Markdown body for the HUD email card — Subject + the real body, so Zack
    reads the actual message (it scrolls on glass). Sender goes in the title."""
    subject = (ctx.get("subject") or "(no subject)").strip()
    body = _clip(ctx.get("body"), _BODY_CARD_CAP) or "(empty body)"
    return f"**{subject}**\n\n{body}"


def card_title(ctx: dict[str, Any]) -> str:
    who = (ctx.get("sender_name") or ctx.get("sender_email") or "Unknown").strip()
    return f"✉ {who}"


def context_block(ctx: dict[str, Any], instruction: str) -> str:
    """Build the augmented ask_text handed to the normal classifier/dispatch
    pipeline: the inbound email as context + Zack's spoken instruction + an
    explicit nudge to reply via reply_to_message_id (so it threads). This is
    the SAME augment-the-ask_text pattern used by cross-session context_from.
    """
    sender = (ctx.get("sender_name") or "").strip()
    addr = (ctx.get("sender_email") or "").strip()
    who = f"{sender} <{addr}>".strip() if sender and addr else (sender or addr or "(unknown)")
    subject = (ctx.get("subject") or "(no subject)").strip()
    msg_id = (ctx.get("message_id") or "").strip()
    account = (ctx.get("account") or "").strip()
    body = _clip(ctx.get("body"), _BODY_CTX_CAP)

    L: list[str] = []
    L.append("[INBOUND EMAIL — this is the message Zack is replying to]")
    L.append(f"From: {who}")
    L.append(f"Subject: {subject}")
    if msg_id:
        L.append(f"Message-ID: {msg_id}")
    if account:
        L.append(f"Account: {account}")
    L.append("")
    L.append("--- email body ---")
    L.append(body or "(empty body)")
    L.append("--- end body ---")
    L.append("")
    L.append(f'Zack\'s instruction: "{instruction.strip()}"')
    if msg_id:
        L.append("")
        L.append(
            f'To send the reply, propose an email action with '
            f'reply_to_message_id="{msg_id}" and body=<your reply> — it threads '
            f'to this message automatically (no "to"/"subject" needed).')
    return "\n".join(L)
