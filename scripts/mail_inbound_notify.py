#!/usr/bin/env python3
"""Mail.app rule → cortex bridge (Zack 2026-06-05).

A Mail.app "Run AppleScript" rule fires this on each new inbound message and
POSTs it to cortex's /api/mail/inbound, which (VIP-filtered, glasses-connected)
pushes a HUD card Zack can long-press to dictate a reply.

WHY a separate script (not curl inside AppleScript): building correct JSON for an
arbitrary email body (newlines, quotes, unicode) inside AppleScript is a footgun.
AppleScript hands us the 4 short fields as argv and the BODY on stdin (via a
heredoc), and we do the JSON + HTTP safely with the stdlib.

Usage (from the Mail rule — see docs/server/MAIL-INBOUND-RULE.md):
    mail_inbound_notify.py <message_id> <sender> <subject> <account> <body_file>

The body is passed as a TEMP FILE PATH (argv[5]), NOT on stdin — reading stdin
inside Mail's `do shell script` could block forever and freeze Mail. We read the
file and delete it. (If no body_file is given we fall back to stdin only when it's
a pipe, for the manual `printf | …` test.) The Mail rule launches this DETACHED
(`… &`) so Mail never waits on us regardless.

Cortex endpoint: env CONSTELLATION_CORTEX_HTTP (default http://<mac-host>:8890).
This runs in Zack's GUI login session (the Mail rule context), which already has
Automation/TCC for Mail — so it sidesteps the launchd-TCC limitation. Stays silent
+ exits 0 on any error so a hiccup never blocks Mail's rule processing.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

CORTEX = os.environ.get("CONSTELLATION_CORTEX_HTTP", "http://<mac-host>:8890").rstrip("/")
TIMEOUT_S = 4.0


def _read_body(args: list[str]) -> str:
    """Body from the temp file at argv[5] (preferred — never blocks); else from a
    piped stdin (manual test only). Never block on a tty stdin."""
    if len(args) > 4 and args[4]:
        path = args[4]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    if not sys.stdin.isatty():
        try:
            return sys.stdin.read()
        except OSError:
            return ""
    return ""


def main() -> int:
    args = sys.argv[1:]
    message_id = args[0] if len(args) > 0 else ""
    sender = args[1] if len(args) > 1 else ""      # "Name <addr@x>" or bare addr
    subject = args[2] if len(args) > 2 else ""
    account = args[3] if len(args) > 3 else ""
    body = _read_body(args)

    if not message_id or not sender:
        # Nothing to thread to / nobody to attribute — skip quietly.
        return 0

    # Split "Name <addr>" → name + addr. cortex re-extracts the bare addr for the
    # VIP check, so passing the whole sender string as sender_email is also fine;
    # we split here just to populate a nicer card title.
    sender_name, sender_email = sender, sender
    if "<" in sender and ">" in sender:
        sender_name = sender.split("<", 1)[0].strip().strip('"')
        sender_email = sender.split("<", 1)[1].split(">", 1)[0].strip()

    payload = json.dumps({
        "message_id": message_id,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": subject,
        "body": body,
        "account": account,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{CORTEX}/api/mail/inbound", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            resp.read()
    except (urllib.error.URLError, OSError):
        # Cortex down / glasses offline / slow — never block Mail. Drop silently.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
