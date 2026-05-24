"""imessage adapter — macOS Messages.app via AppleScript (send) + chat.db (read).

Actions: send, list_recent.

Side-effects: send=medium (irreversible), list_recent=none.

`send` uses AppleScript `tell application "Messages"` to dispatch through iMessage; pass
phone number (e.g. "+12025551234") or Apple ID email.

`list_recent` reads `~/Library/Messages/chat.db` SQLite (Apple's iMessage store). This
requires **Full Disk Access** TCC for the cortex Python binary. If FDA isn't granted,
returns a soft error (not a crash).

NOTE: no `read_message_by_id` or `reply_to_message` v1 — Messages.app's AppleScript
surface doesn't expose a clean reply primitive. To respond, just `send` to the same buddy.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
APPLE_EPOCH_NS = 978307200_000_000_000  # 2001-01-01 UTC in ns since 1970


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _apple_ns_to_iso(ns: int) -> str:
    if ns == 0:
        return ""
    epoch_ns = ns + APPLE_EPOCH_NS
    secs = epoch_ns / 1_000_000_000
    return datetime.fromtimestamp(secs, tz=timezone.utc).astimezone().isoformat()


async def _run_osascript(script: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8"), err.decode("utf-8")


class IMessageAdapter:
    name = "imessage"

    SIDE_EFFECT_ACTIONS = {"send"}

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        if action in self.SIDE_EFFECT_ACTIONS and result_format == "draft":
            raise ValueError(
                f"imessage.{action} has no draft semantics — sent messages can't be unsent."
            )

        if action == "send":
            return await self._send(args)
        if action == "list_recent":
            return await self._list_recent(args)
        raise ValueError(f"imessage: unknown action '{action}'")

    # ── send ──

    async def _send(self, args: dict[str, Any]) -> dict[str, Any]:
        to = args.get("to") or args.get("buddy") or args.get("recipient")
        body = args.get("body") or args.get("message")
        if not to or body is None:
            raise ValueError("imessage.send: 'to' (phone/email) and 'body' required")

        # iMessage service; fall back to SMS via "SMS" service if explicitly requested
        service = args.get("service", "iMessage")
        script = f'''
        tell application "Messages"
            set targetService to 1st service whose service type = {service}
            set theBuddy to buddy "{_esc(to)}" of targetService
            send "{_esc(body)}" to theBuddy
            return ""
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"imessage.send failed (rc={rc}): {stderr.strip()}")
        return {"to": to, "service": service, "body_chars": len(body), "sent": True}

    # ── read ──

    async def _list_recent(self, args: dict[str, Any]) -> dict[str, Any]:
        if not CHAT_DB.exists():
            return {"error": f"chat.db not found at {CHAT_DB}", "items": []}

        limit = int(args.get("limit", 20))
        contact_filter = args.get("contact")  # phone/email substring
        since_hours = args.get("since_hours")

        try:
            # Open read-only via URI to avoid lockfile contention
            conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.OperationalError as e:
            return {
                "error": (
                    f"cannot open chat.db: {e}. macOS Full Disk Access TCC permission "
                    "is required for the cortex python binary."
                ),
                "items": [],
            }

        try:
            where = []
            params: list[Any] = []
            if since_hours is not None:
                cutoff_ns = int((datetime.now(tz=timezone.utc) - timedelta(hours=float(since_hours))).timestamp() * 1_000_000_000) - APPLE_EPOCH_NS
                where.append("m.date > ?")
                params.append(cutoff_ns)
            if contact_filter:
                where.append("(h.id LIKE ?)")
                params.append(f"%{contact_filter}%")
            where_sql = "WHERE " + " AND ".join(where) if where else ""

            sql = f"""
              SELECT m.ROWID as rowid,
                     m.date as date_ns,
                     m.text,
                     m.is_from_me,
                     h.id as contact
              FROM message m
              LEFT JOIN handle h ON m.handle_id = h.ROWID
              {where_sql}
              ORDER BY m.date DESC
              LIMIT ?
            """
            cur = conn.execute(sql, params + [limit])
            items = []
            for row in cur.fetchall():
                items.append({
                    "rowid": row["rowid"],
                    "date": _apple_ns_to_iso(row["date_ns"]),
                    "from_me": bool(row["is_from_me"]),
                    "contact": row["contact"] or "",
                    "text": row["text"] or "",
                })
        finally:
            conn.close()

        return {
            "filters": {"contact": contact_filter, "since_hours": since_hours},
            "count": len(items),
            "items": items,
        }
