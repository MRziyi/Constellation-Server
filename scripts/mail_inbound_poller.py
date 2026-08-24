#!/usr/bin/env python3
"""Inbound-email poller → cortex HUD push (Zack 2026-06-06).

The Mail "Run AppleScript" rule proved unreliable on Gmail/IMAP accounts (it
silently does not fire on much synced mail). This poller does NOT depend on
Mail's rule engine: it ACTIVELY reads the inbox every tick, diffs against a
seen-set, and POSTs genuinely-new messages to cortex /api/mail/inbound (which
applies the VIP gate + glasses-connected gate). A storm is structurally
impossible — one bounded query per tick, capped message count, recency-windowed.

Runs in Zack's GUI login session (a LaunchAgent, Aqua) so it has Mail's
Automation/TCC. Read-only against Mail; the only side effect is the POST to
your own cortex. Safe to run by hand for testing.

Usage:
    mail_inbound_poller.py            # one tick: push new mail since last seen
    mail_inbound_poller.py --prime    # record current inbox as seen, push nothing
    mail_inbound_poller.py --push-all # ignore seen-set, push all in-window (test)

Env: CONSTELLATION_CORTEX_HTTP (default http://<mac-host>:8890),
     CONSTELLATION_MAIL_LOOKBACK_MIN (default 30), CONSTELLATION_MAIL_MAX (default 15).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

CORTEX = os.environ.get("CONSTELLATION_CORTEX_HTTP", "http://<mac-host>:8890").rstrip("/")
LOOKBACK_MIN = int(os.environ.get("CONSTELLATION_MAIL_LOOKBACK_MIN", "30"))
MAX_MSGS = int(os.environ.get("CONSTELLATION_MAIL_MAX", "15"))
SEEN_PATH = os.path.expanduser("~/constellation/twin/_system/.mail_poll_seen.json")
SEEN_CAP = 500
FS = "\x1f"   # field sep
RS = "\x1e"   # record sep

# One bounded Mail query: recent INBOX messages across accounts, newest-ish first,
# capped at MAX_MSGS. Returns id / sender / subject / account / body per record.
_OSA = f'''
tell application "Mail"
  set cutoff to (current date) - ({LOOKBACK_MIN} * minutes)
  set out to ""
  set n to 0
  repeat with acc in every account
    if n < {MAX_MSGS} then
      try
        set ib to mailbox "INBOX" of acc
      on error
        try
          set ib to inbox of acc
        on error
          set ib to missing value
        end try
      end try
      if ib is not missing value then
        try
          repeat with m in (messages of ib whose date received > cutoff)
            if n < {MAX_MSGS} then
              set out to out & (message id of m) & "{FS}" & (sender of m) & "{FS}" & (subject of m) & "{FS}" & (name of acc) & "{FS}" & (content of m) & "{RS}"
              set n to n + 1
            end if
          end repeat
        end try
      end if
    end if
  end repeat
  return out
end tell
'''


def _query_inbox() -> list[dict]:
    try:
        p = subprocess.run(["osascript", "-e", _OSA], capture_output=True,
                           text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if p.returncode != 0:
        return []
    out: list[dict] = []
    for rec in p.stdout.split(RS):
        if not rec.strip():
            continue
        f = rec.split(FS)
        if len(f) < 5:
            continue
        mid, sender, subject, account, body = f[0], f[1], f[2], f[3], FS.join(f[4:])
        if not mid:
            continue
        out.append({"message_id": mid, "sender": sender, "subject": subject,
                    "account": account, "body": body})
    return out


def _load_seen() -> set[str]:
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as fh:
            return set(json.load(fh))
    except (OSError, ValueError):
        return set()


def _save_seen(seen: set[str]) -> None:
    ids = list(seen)[-SEEN_CAP:]
    try:
        os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
        with open(SEEN_PATH, "w", encoding="utf-8") as fh:
            json.dump(ids, fh)
    except OSError:
        pass


def _post(msg: dict) -> str:
    sender = msg["sender"]
    name, addr = sender, sender
    if "<" in sender and ">" in sender:
        name = sender.split("<", 1)[0].strip().strip('"')
        addr = sender.split("<", 1)[1].split(">", 1)[0].strip()
    payload = json.dumps({
        "message_id": msg["message_id"], "sender_name": name, "sender_email": addr,
        "subject": msg["subject"], "body": msg["body"], "account": msg["account"],
    }).encode("utf-8")
    req = urllib.request.Request(f"{CORTEX}/api/mail/inbound", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.read().decode()
    except (urllib.error.URLError, OSError) as e:
        return f"(post failed: {e})"


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    msgs = _query_inbox()
    seen = _load_seen()
    seen_existed = os.path.exists(SEEN_PATH)

    if mode == "--prime" or (not seen_existed and mode != "--push-all"):
        # First run: adopt the current inbox as the baseline so we don't push a
        # backlog the moment the poller is installed.
        for m in msgs:
            seen.add(m["message_id"])
        _save_seen(seen)
        print(f"primed: {len(msgs)} message(s) marked seen, none pushed")
        return 0

    new = [m for m in msgs if mode == "--push-all" or m["message_id"] not in seen]
    print(f"in-window={len(msgs)}  new={len(new)}")
    for m in new:
        resp = _post(m)
        seen.add(m["message_id"])
        print(f"  → {m['sender'][:40]:40} | {m['subject'][:40]:40} | cortex: {resp}")
    _save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
