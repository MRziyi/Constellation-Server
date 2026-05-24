"""safari_state adapter — current Safari tab + recent history.

Actions: current_tab, all_tabs, recent_history.

Side-effects: none for all actions.

`current_tab` / `all_tabs` use AppleScript (works with basic TCC for System Events).
`recent_history` reads `~/Library/Safari/History.db` SQLite — requires **Full Disk Access**
TCC for the cortex python binary. If FDA isn't granted, returns soft error.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HISTORY_DB = Path.home() / "Library" / "Safari" / "History.db"
# Safari uses Apple "Cocoa" epoch (2001-01-01 UTC) for timestamps in seconds.
COCOA_EPOCH_OFFSET = 978307200.0


async def _run_osascript(script: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8"), err.decode("utf-8")


def _cocoa_ts_to_iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts + COCOA_EPOCH_OFFSET, tz=timezone.utc).astimezone().isoformat()


class SafariStateAdapter:
    name = "safari_state"

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        if action == "current_tab":
            return await self._current_tab(args)
        if action == "all_tabs":
            return await self._all_tabs(args)
        if action == "recent_history":
            return await self._recent_history(args)
        raise ValueError(f"safari_state: unknown action '{action}'")

    async def _current_tab(self, args: dict[str, Any]) -> dict[str, Any]:
        script = '''
        tell application "Safari"
            if (count of windows) = 0 then return ""
            set fs to (ASCII character 31)
            set t to current tab of front window
            return (URL of t) & fs & (name of t)
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            return {"error": stderr.strip(), "found": False}
        if not stdout.strip():
            return {"found": False, "message": "Safari has no open windows."}
        parts = stdout.split("\x1f")
        return {
            "found": True,
            "url": parts[0] if len(parts) > 0 else "",
            "title": parts[1].strip() if len(parts) > 1 else "",
        }

    async def _all_tabs(self, args: dict[str, Any]) -> dict[str, Any]:
        script = '''
        tell application "Safari"
            set fs to (ASCII character 31)
            set rs to (ASCII character 30)
            set out to ""
            repeat with w in windows
                repeat with t in tabs of w
                    set out to out & (URL of t) & fs & (name of t) & rs
                end repeat
            end repeat
            return out
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            return {"error": stderr.strip(), "items": []}
        items = []
        for rec in stdout.strip().split("\x1e"):
            rec = rec.strip()
            if not rec:
                continue
            parts = rec.split("\x1f")
            if len(parts) < 2:
                continue
            items.append({"url": parts[0], "title": parts[1].strip()})
        return {"count": len(items), "items": items}

    async def _recent_history(self, args: dict[str, Any]) -> dict[str, Any]:
        if not HISTORY_DB.exists():
            return {"error": f"History.db not found at {HISTORY_DB}", "items": []}
        hours = float(args.get("hours", 24))
        limit = int(args.get("limit", 30))
        cutoff = datetime.now(tz=timezone.utc).timestamp() - hours * 3600 - COCOA_EPOCH_OFFSET

        try:
            conn = sqlite3.connect(f"file:{HISTORY_DB}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.OperationalError as e:
            return {
                "error": (
                    f"cannot open History.db: {e}. macOS Full Disk Access TCC permission "
                    "is required for the cortex python binary."
                ),
                "items": [],
            }

        try:
            sql = """
              SELECT hi.url, hv.title, hv.visit_time
              FROM history_visits hv
              JOIN history_items hi ON hv.history_item = hi.id
              WHERE hv.visit_time > ?
              ORDER BY hv.visit_time DESC
              LIMIT ?
            """
            cur = conn.execute(sql, (cutoff, limit))
            items = []
            for row in cur.fetchall():
                items.append({
                    "url": row["url"],
                    "title": row["title"] or "",
                    "visited": _cocoa_ts_to_iso(row["visit_time"]),
                })
        finally:
            conn.close()

        return {"hours": hours, "count": len(items), "items": items}
