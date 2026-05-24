"""applescript_reminders adapter — macOS Reminders.app via osascript.

Actions: add, list, complete, delete.

See TOOL-ADAPTERS.md §4. Side-effects: read=none, add=low, delete=medium.

Mechanism: each action shells out to `osascript -e ...`. AppleScript snippets are written
to be defensive (graceful on missing list, etc.) and return JSON-via-stdout that the
adapter parses.

Date handling: `due` is accepted as a plain string. We pass it verbatim to AppleScript's
`date "..."` parser, which accepts forms like "May 31, 2026 9:00 AM" or "tomorrow 3pm" on
recent macOS. If your Router emits ISO 8601, the adapter normalizes via a small helper.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from datetime import datetime
from typing import Any


def _applescript_escape(s: str) -> str:
    """Escape a string for safe embedding inside an AppleScript "..." literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _normalize_due(due: str) -> str:
    """Convert ISO 8601 to AppleScript-friendly "Month D, YYYY H:MM AM/PM".

    AppleScript's `date "..."` is locale-tolerant but more reliable with the long form.
    If `due` doesn't look like ISO, return it unchanged (let AppleScript try its parser).
    """
    iso_pat = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
    if not iso_pat.match(due):
        return due
    try:
        # Tolerate trailing Z or offset
        s = due.rstrip("Z")
        dt = datetime.fromisoformat(s)
        # AppleScript date string format that works across locales
        return dt.strftime("%B %-d, %Y %-I:%M %p")
    except ValueError:
        return due


async def _run_osascript(script: str) -> tuple[int, str, str]:
    """Run an AppleScript snippet via `osascript -e`. Returns (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "osascript",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    return proc.returncode or 0, stdout_b.decode("utf-8"), stderr_b.decode("utf-8")


class RemindersAdapter:
    name = "applescript_reminders"

    SIDE_EFFECT_ACTIONS = {"add", "complete", "delete"}

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        # Defense in depth: side-effecting actions MUST NOT run at preview time.
        # If Router emits result_format="draft" for one of these, the adapter refuses.
        # The Router prompt also warns about this; this guard is for when it slips.
        if action in self.SIDE_EFFECT_ACTIONS and result_format == "draft":
            raise ValueError(
                f"applescript_reminders.{action} has no draft semantics. "
                f"Router must use result_format='execute' with requires_confirm=true."
            )

        if action == "add":
            return await self._add(args)
        if action == "list":
            return await self._list(args)
        if action == "complete":
            return await self._complete(args)
        if action == "delete":
            return await self._delete(args)
        raise ValueError(f"applescript_reminders: unknown action '{action}'")

    # ── actions ──

    async def _add(self, args: dict[str, Any]) -> dict[str, Any]:
        title = args.get("title")
        if not title:
            raise ValueError("applescript_reminders.add: 'title' is required")

        list_name = args.get("list") or "Reminders"
        notes = args.get("notes") or ""
        due = args.get("due")

        # Build AppleScript with optional due date / notes properties.
        props_parts = [f'name:"{_applescript_escape(title)}"']
        if notes:
            props_parts.append(f'body:"{_applescript_escape(notes)}"')
        if due:
            normalized = _normalize_due(due)
            props_parts.append(f'due date:(date "{_applescript_escape(normalized)}")')
        props_block = "{" + ", ".join(props_parts) + "}"

        script = f'''
        tell application "Reminders"
            if not (exists list "{_applescript_escape(list_name)}") then
                make new list with properties {{name:"{_applescript_escape(list_name)}"}}
            end if
            set newReminder to make new reminder at list "{_applescript_escape(list_name)}" with properties {props_block}
            return id of newReminder
        end tell
        '''

        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        reminder_id = stdout.strip()
        return {
            "reminder_id": reminder_id,
            "title": title,
            "list": list_name,
            "due": due,
        }

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        list_name = args.get("list") or "Reminders"
        completed = bool(args.get("completed", False))
        completed_filter = "true" if completed else "false"

        script = f'''
        tell application "Reminders"
            if not (exists list "{_applescript_escape(list_name)}") then return "[]"
            set theList to list "{_applescript_escape(list_name)}"
            set itemsOut to {{}}
            repeat with r in (reminders of theList whose completed is {completed_filter})
                set end of itemsOut to (id of r) & "\\t" & (name of r)
            end repeat
            set AppleScript's text item delimiters to linefeed
            set joined to itemsOut as text
            return joined
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        items = []
        for line in stdout.strip().splitlines():
            if "\t" in line:
                rid, title = line.split("\t", 1)
                items.append({"reminder_id": rid, "title": title})
        return {"list": list_name, "completed": completed, "items": items}

    async def _complete(self, args: dict[str, Any]) -> dict[str, Any]:
        reminder_id = args.get("reminder_id")
        if not reminder_id:
            raise ValueError("applescript_reminders.complete: 'reminder_id' is required")
        script = f'''
        tell application "Reminders"
            set r to (first reminder whose id is "{_applescript_escape(reminder_id)}")
            set completed of r to true
            return id of r
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        return {"reminder_id": stdout.strip(), "completed": True}

    async def _delete(self, args: dict[str, Any]) -> dict[str, Any]:
        reminder_id = args.get("reminder_id")
        if not reminder_id:
            raise ValueError("applescript_reminders.delete: 'reminder_id' is required")
        script = f'''
        tell application "Reminders"
            delete (first reminder whose id is "{_applescript_escape(reminder_id)}")
            return "ok"
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        return {"reminder_id": reminder_id, "deleted": True}
