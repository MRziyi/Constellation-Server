"""applescript_calendar adapter — macOS Calendar.app via osascript.

Actions: list_today, list_range, add_event, find_conflict, get_event.

See TOOL-ADAPTERS.md §3. Side-effects: read=none, add_event=low.

Mechanism: each action shells out to `osascript -e ...`. Calendar.app's AppleScript surface
returns event uids and basic fields; we marshal these into JSON lines that the adapter parses.

Date handling: caller passes ISO 8601; adapter converts to AppleScript's long date string
(`"Month D, YYYY H:MM:SS AM/PM"`) which parses reliably across locales.

Default calendar: `个人` (Zack's primary writable). Override via `args.calendar`.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

DEFAULT_CALENDAR = "个人"
FIELD_SEP = "\x1f"  # ASCII Unit Separator — unlikely in user text
RECORD_SEP = "\x1e"  # ASCII Record Separator


def _applescript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _iso_to_applescript(iso: str) -> str:
    """Convert ISO 8601 → 'Month D, YYYY H:MM:SS AM/PM' (AppleScript long form)."""
    s = iso.rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"calendar adapter: bad ISO datetime {iso!r}: {e}")
    return dt.strftime("%B %-d, %Y %-I:%M:%S %p")


async def _run_osascript(script: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8"), err.decode("utf-8")


def _parse_events(stdout: str) -> list[dict[str, str]]:
    """Parse FIELD_SEP-delimited records from list scripts.

    Each record: uid | summary | start_date_str | end_date_str | location | calendar_name
    """
    items = []
    for rec in stdout.strip().split(RECORD_SEP):
        rec = rec.strip()
        if not rec:
            continue
        parts = rec.split(FIELD_SEP)
        if len(parts) < 6:
            continue
        uid, summary, start, end, location, calendar = parts[:6]
        items.append({
            "uid": uid,
            "summary": summary,
            "start": start,
            "end": end,
            "location": location,
            "calendar": calendar,
        })
    return items


class CalendarAdapter:
    name = "applescript_calendar"

    SIDE_EFFECT_ACTIONS = {"add_event"}

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        # Defense in depth: don't actually create events on a "draft" call.
        if action in self.SIDE_EFFECT_ACTIONS and result_format == "draft":
            raise ValueError(
                f"applescript_calendar.{action} has no draft semantics. "
                f"Router must use result_format='execute' with requires_confirm=true."
            )

        if action == "list_today":
            return await self._list_today(args)
        if action == "list_range":
            return await self._list_range(args)
        if action == "add_event":
            return await self._add_event(args)
        if action == "find_conflict":
            return await self._find_conflict(args)
        if action == "get_event":
            return await self._get_event(args)
        raise ValueError(f"applescript_calendar: unknown action '{action}'")

    # ── reads ──

    async def _list_range_internal(self, start_iso: str, end_iso: str, calendar: str | None) -> dict[str, Any]:
        start_apple = _iso_to_applescript(start_iso)
        end_apple = _iso_to_applescript(end_iso)
        cal_clause = (
            f'set cals to {{calendar "{_applescript_escape(calendar)}"}}'
            if calendar
            else "set cals to every calendar"
        )
        # AppleScript: gather events whose start_date < end and end_date > start
        script = f'''
        tell application "Calendar"
            set fs to (ASCII character 31)
            set rs to (ASCII character 30)
            {cal_clause}
            set wStart to date "{start_apple}"
            set wEnd to date "{end_apple}"
            set out to ""
            repeat with c in cals
                set theEvents to (every event of c whose start date < wEnd and end date > wStart)
                repeat with e in theEvents
                    set sLoc to ""
                    try
                        set sLoc to location of e
                        if sLoc is missing value then set sLoc to ""
                    end try
                    set out to out & (uid of e) & fs & (summary of e) & fs & ((start date of e) as string) & fs & ((end date of e) as string) & fs & sLoc & fs & (name of c) & rs
                end repeat
            end repeat
            return out
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        items = _parse_events(stdout)
        return {"range_start": start_iso, "range_end": end_iso, "events": items, "count": len(items)}

    async def _list_today(self, args: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now().astimezone()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(hour=23, minute=59, second=59)
        return await self._list_range_internal(start.isoformat(), end.isoformat(), args.get("calendar"))

    async def _list_range(self, args: dict[str, Any]) -> dict[str, Any]:
        start = args.get("start") or args.get("start_iso")
        end = args.get("end") or args.get("end_iso")
        if not start or not end:
            raise ValueError("applescript_calendar.list_range: 'start' and 'end' (ISO) required")
        return await self._list_range_internal(start, end, args.get("calendar"))

    async def _find_conflict(self, args: dict[str, Any]) -> dict[str, Any]:
        # Same as list_range but framing the result as conflicts.
        result = await self._list_range(args)
        return {
            "window_start": result["range_start"],
            "window_end": result["range_end"],
            "conflicts": result["events"],
            "has_conflict": result["count"] > 0,
        }

    async def _get_event(self, args: dict[str, Any]) -> dict[str, Any]:
        uid = args.get("uid") or args.get("event_id")
        if not uid:
            raise ValueError("applescript_calendar.get_event: 'uid' required")
        cal = args.get("calendar")
        cal_clause = (
            f'set cals to {{calendar "{_applescript_escape(cal)}"}}'
            if cal
            else "set cals to every calendar"
        )
        script = f'''
        tell application "Calendar"
            set fs to (ASCII character 31)
            set rs to (ASCII character 30)
            {cal_clause}
            repeat with c in cals
                try
                    set e to (first event of c whose uid is "{_applescript_escape(uid)}")
                    set sLoc to ""
                    try
                        set sLoc to location of e
                        if sLoc is missing value then set sLoc to ""
                    end try
                    return (uid of e) & fs & (summary of e) & fs & ((start date of e) as string) & fs & ((end date of e) as string) & fs & sLoc & fs & (name of c) & rs
                end try
            end repeat
            return ""
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        items = _parse_events(stdout)
        if not items:
            return {"uid": uid, "found": False}
        return {**items[0], "found": True}

    # ── writes ──

    async def _add_event(self, args: dict[str, Any]) -> dict[str, Any]:
        title = args.get("title") or args.get("summary")
        start = args.get("start") or args.get("start_iso")
        end = args.get("end") or args.get("end_iso")
        if not title or not start or not end:
            raise ValueError(
                "applescript_calendar.add_event: 'title', 'start' (ISO), 'end' (ISO) required"
            )
        calendar = args.get("calendar") or DEFAULT_CALENDAR
        location = args.get("location") or ""
        notes = args.get("notes") or ""

        start_apple = _iso_to_applescript(start)
        end_apple = _iso_to_applescript(end)

        props = [
            f'summary:"{_applescript_escape(title)}"',
            f'start date:(date "{_applescript_escape(start_apple)}")',
            f'end date:(date "{_applescript_escape(end_apple)}")',
        ]
        if location:
            props.append(f'location:"{_applescript_escape(location)}"')
        if notes:
            props.append(f'description:"{_applescript_escape(notes)}"')
        props_block = "{" + ", ".join(props) + "}"

        script = f'''
        tell application "Calendar"
            tell calendar "{_applescript_escape(calendar)}"
                set newEvent to make new event with properties {props_block}
                return uid of newEvent
            end tell
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        return {
            "uid": stdout.strip(),
            "title": title,
            "start": start,
            "end": end,
            "calendar": calendar,
            "location": location,
        }
