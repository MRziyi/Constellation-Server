"""apple_notes adapter — macOS Notes.app via osascript.

Actions: create, list, read, append, search.

Notes.app stores rich text; AppleScript's `body` returns HTML. We surface `plaintext` (best-
effort by reading `body of note` which AppleScript renders as plain text in modern macOS).

Folder model: Notes is organized into folders (per account). Default account = "iCloud" for
Zack's setup; default folder = "Notes". Override via args.account / args.folder.

Side-effects:
- create  : low (new note appears in Notes.app; trivially deletable)
- append  : low
- list    : none
- read    : none
- search  : none

No delete action in v1 — Notes is "drop a thought" surface; users should remove notes
manually in the app to avoid surprise data loss.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any


FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"

DEFAULT_ACCOUNT = "iCloud"
DEFAULT_FOLDER = "Notes"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def _run_osascript(script: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8"), err.decode("utf-8")


_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    # Cheap HTML→plaintext for note bodies. Notes wraps lines in <div>; turn them into \n.
    s = re.sub(r"</div>\s*<div>", "\n", s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = _HTML_TAG.sub("", s)
    return s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


class AppleNotesAdapter:
    name = "apple_notes"

    SIDE_EFFECT_ACTIONS = {"create", "append"}

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        if action in self.SIDE_EFFECT_ACTIONS and result_format == "draft":
            raise ValueError(
                f"apple_notes.{action} has no draft semantics. "
                f"Use result_format='execute' (note creation is low-risk + reversible in Notes.app)."
            )

        if action == "create":
            return await self._create(args)
        if action == "list":
            return await self._list(args)
        if action == "read":
            return await self._read(args)
        if action == "append":
            return await self._append(args)
        if action == "search":
            return await self._search(args)
        raise ValueError(f"apple_notes: unknown action '{action}'")

    # ── writes ──

    async def _create(self, args: dict[str, Any]) -> dict[str, Any]:
        title = args.get("title") or args.get("name")
        body = args.get("body") or args.get("content") or ""
        if not title:
            raise ValueError("apple_notes.create: 'title' required")
        account = args.get("account") or DEFAULT_ACCOUNT
        folder = args.get("folder") or DEFAULT_FOLDER

        # Notes' `body` accepts HTML; we put title as <h1> + body as <div>s.
        body_html = "<h1>" + _esc(title) + "</h1>" + "".join(
            f"<div>{_esc(line)}</div>" for line in body.split("\n")
        )

        script = f'''
        tell application "Notes"
            tell account "{_esc(account)}"
                set targetFolder to folder "{_esc(folder)}"
                set newNote to make new note at targetFolder with properties {{name:"{_esc(title)}", body:"{body_html}"}}
                return (id of newNote as string) & "{FIELD_SEP}" & (name of newNote)
            end tell
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        parts = stdout.strip().split(FIELD_SEP)
        return {
            "note_id": parts[0] if parts else "",
            "title": parts[1] if len(parts) > 1 else title,
            "account": account,
            "folder": folder,
        }

    async def _append(self, args: dict[str, Any]) -> dict[str, Any]:
        note_id = args.get("note_id") or args.get("id")
        content = args.get("content") or args.get("body")
        if not note_id or content is None:
            raise ValueError("apple_notes.append: 'note_id' and 'content' required")
        # Build appended HTML; we read current body then write back. Notes doesn't expose a
        # native "append to body" — we replace `body`.
        append_html = "".join(f"<div>{_esc(line)}</div>" for line in str(content).split("\n"))
        script = f'''
        tell application "Notes"
            set n to (first note whose id is "{_esc(note_id)}")
            set oldBody to body of n
            set body of n to (oldBody & "{append_html}")
            return id of n as string
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        return {"note_id": stdout.strip(), "appended_chars": len(str(content))}

    # ── reads ──

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        account = args.get("account") or DEFAULT_ACCOUNT
        folder = args.get("folder") or DEFAULT_FOLDER
        limit = int(args.get("limit", 30))
        script = f'''
        tell application "Notes"
            set fs to (ASCII character 31)
            set rs to (ASCII character 30)
            tell account "{_esc(account)}"
                set theNotes to notes of folder "{_esc(folder)}"
                set out to ""
                set i to 0
                repeat with n in theNotes
                    if i ≥ {limit} then exit repeat
                    set out to out & (id of n as string) & fs & (name of n) & fs & ((modification date of n) as string) & rs
                    set i to i + 1
                end repeat
                return out
            end tell
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        items = []
        for rec in stdout.strip().split(RECORD_SEP):
            rec = rec.strip()
            if not rec:
                continue
            parts = rec.split(FIELD_SEP)
            if len(parts) < 3:
                continue
            items.append({"note_id": parts[0], "title": parts[1], "modified": parts[2]})
        return {"account": account, "folder": folder, "count": len(items), "items": items}

    async def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        note_id = args.get("note_id") or args.get("id")
        if not note_id:
            raise ValueError("apple_notes.read: 'note_id' required")
        script = f'''
        tell application "Notes"
            set n to (first note whose id is "{_esc(note_id)}")
            set fs to (ASCII character 31)
            return (id of n as string) & fs & (name of n) & fs & (body of n) & fs & ((modification date of n) as string)
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        parts = stdout.split(FIELD_SEP)
        if len(parts) < 4:
            return {"note_id": note_id, "found": False}
        return {
            "note_id": parts[0],
            "title": parts[1],
            "body_html": parts[2],
            "plaintext": _strip_html(parts[2]),
            "modified": parts[3].strip(),
            "found": True,
        }

    async def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query") or args.get("q")
        if not query:
            raise ValueError("apple_notes.search: 'query' required")
        limit = int(args.get("limit", 20))
        # Notes' AppleScript surface doesn't have a great search; we filter `name contains`
        # across all accounts/folders.
        script = f'''
        tell application "Notes"
            set fs to (ASCII character 31)
            set rs to (ASCII character 30)
            set out to ""
            set i to 0
            repeat with n in (notes whose name contains "{_esc(query)}")
                if i ≥ {limit} then exit repeat
                set out to out & (id of n as string) & fs & (name of n) & fs & ((modification date of n) as string) & rs
                set i to i + 1
            end repeat
            return out
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        items = []
        for rec in stdout.strip().split(RECORD_SEP):
            rec = rec.strip()
            if not rec:
                continue
            parts = rec.split(FIELD_SEP)
            if len(parts) < 3:
                continue
            items.append({"note_id": parts[0], "title": parts[1], "modified": parts[2]})
        return {"query": query, "count": len(items), "items": items, "note": "v1 title-only search; body search needs SQLite path or scripting bridge"}
