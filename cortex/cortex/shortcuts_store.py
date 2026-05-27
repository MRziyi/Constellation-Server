"""
shortcuts_store — Twin-backed CRUD for `skills/shortcuts.md` blocks.

A shortcut is a one-tap fire-and-forget invocation: a preset prompt
(optionally bundled with a fresh camera frame) that fires to Cortex
without any voice step. By design there is **no mic** field — if a
trigger should open the mic, that's a normal voice invoke, not a
shortcut.

Schema (defined in `twin-seed/skills/shortcuts.md`):
    <!-- shortcut:start -->
    id: kebab-case-id
    name: Human-readable
    photo: true|false
    created: 2026-05-26
    updated: 2026-05-26
    <!-- shortcut:body -->

    <prompt body>
    <!-- shortcut:end -->

We use HTML-comment markers (not YAML frontmatter per block) because
frontmatter only works at file start. Comments are invisible in rendered
markdown, easy to grep, and trivially parseable.

The store is **stateless** — every read parses the whole file, every write
rewrites it. shortcuts.md is small (handful of entries × a few hundred
chars each), so this stays cheap. The benefit: no in-memory cache to
invalidate when a user hand-edits the file outside the app.

Used by:
  - HTTP API: `/api/shortcuts` GET/POST/PUT/DELETE in cortex.http
  - Router context (future): exposing shortcuts as known intents
  - Halo Ring plugin protocol (Glass side): `HaloActionsProvider` cursor
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


SHORTCUTS_RELPATH = "skills/shortcuts.md"

# Block delimiters — must match the schema in shortcuts.md
START = "<!-- shortcut:start -->"
BODY = "<!-- shortcut:body -->"
END = "<!-- shortcut:end -->"

_BLOCK_RE = re.compile(
    rf"{re.escape(START)}\s*\n(?P<header>.*?){re.escape(BODY)}\s*\n(?P<body>.*?){re.escape(END)}",
    re.DOTALL,
)

# A conservative id regex matching what Glass's editor enforces (lowercase + digits + hyphen).
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")


@dataclass
class Shortcut:
    id: str
    name: str
    prompt: str
    photo: bool = False     # capture a fresh camera frame and bundle with the prompt
    created: str = ""       # ISO date; populated on first save
    updated: str = ""       # ISO date

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Shortcut":
        return Shortcut(
            id=str(d["id"]),
            name=str(d.get("name", "")),
            prompt=str(d.get("prompt", "")),
            photo=bool(d.get("photo", False)),
            created=str(d.get("created", "")),
            updated=str(d.get("updated", "")),
        )


class ShortcutsStore:
    """Reader + writer for shortcuts.md. `twin` is a `cortex.twin.Twin` instance."""

    def __init__(self, twin: Any):
        self.twin = twin

    # ── Reads ──────────────────────────────────────────────────────────────

    def list(self) -> list[Shortcut]:
        if not self.twin.exists(SHORTCUTS_RELPATH):
            return []
        text = self.twin.read(SHORTCUTS_RELPATH)
        return list(self._parse(text))

    def get(self, sid: str) -> Shortcut | None:
        return next((s for s in self.list() if s.id == sid), None)

    # ── Writes ─────────────────────────────────────────────────────────────

    def create(self, payload: dict[str, Any]) -> Shortcut:
        sid = payload.get("id") or ""
        if not _ID_RE.match(sid):
            raise ValueError(f"invalid id '{sid}' — must match {_ID_RE.pattern}")
        existing = self.list()
        if any(s.id == sid for s in existing):
            raise ValueError(f"shortcut id '{sid}' already exists")
        today = date.today().isoformat()
        sc = Shortcut(
            id=sid,
            name=str(payload.get("name", "")).strip() or sid,
            prompt=str(payload.get("prompt", "")).strip(),
            photo=bool(payload.get("photo", False)),
            created=today,
            updated=today,
        )
        self._rewrite_file(existing + [sc])
        return sc

    def update(self, sid: str, payload: dict[str, Any]) -> Shortcut | None:
        existing = self.list()
        idx = next((i for i, s in enumerate(existing) if s.id == sid), -1)
        if idx < 0:
            return None
        old = existing[idx]
        today = date.today().isoformat()
        merged = Shortcut(
            id=old.id,  # id is immutable
            name=str(payload.get("name", old.name)).strip() or old.name,
            prompt=str(payload.get("prompt", old.prompt)).strip(),
            photo=bool(payload.get("photo", old.photo)),
            created=old.created or today,
            updated=today,
        )
        existing[idx] = merged
        self._rewrite_file(existing)
        return merged

    def delete(self, sid: str) -> bool:
        existing = self.list()
        kept = [s for s in existing if s.id != sid]
        if len(kept) == len(existing):
            return False
        self._rewrite_file(kept)
        return True

    # ── Internals ──────────────────────────────────────────────────────────

    def _parse(self, text: str) -> list[Shortcut]:
        # Strip ```...``` code fences first — the schema documentation in
        # shortcuts.md uses literal `<!-- shortcut:start -->` examples inside
        # ```markdown code blocks``` which would otherwise be parsed as real
        # shortcuts. Stripping fenced blocks for parsing-only is safe; the
        # _rewrite_file path always rewrites from the in-memory list so the
        # docs aren't touched.
        stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        out: list[Shortcut] = []
        for m in _BLOCK_RE.finditer(stripped):
            header = m.group("header")
            body = m.group("body").strip()
            fields: dict[str, str] = {}
            for line in header.splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                fields[k.strip()] = v.strip()
            sid = fields.get("id", "").strip()
            if not sid:
                continue  # corrupt block — skip rather than raise
            out.append(Shortcut(
                id=sid,
                name=fields.get("name", sid),
                prompt=body,
                photo=fields.get("photo", "false").lower() == "true",
                created=fields.get("created", ""),
                updated=fields.get("updated", ""),
            ))
        return out

    def _rewrite_file(self, shortcuts: list[Shortcut]) -> None:
        """Rewrite shortcuts.md preserving the leading header (anything before
        the first <!-- shortcut:start --> block) verbatim, then re-emitting
        every block. Hand-edited free prose above the first block is kept."""
        path = self.twin.root / SHORTCUTS_RELPATH
        existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
        # Header = everything up to the first START marker; if no markers, treat
        # entire file as header (preserves a file with only doc/comments).
        first = existing_text.find(START)
        header = existing_text[:first] if first >= 0 else existing_text
        # Append a trailing newline if the header doesn't end with one.
        if header and not header.endswith("\n"):
            header += "\n"
        blocks = [self._render_block(s) for s in shortcuts]
        new_text = header + ("\n".join(blocks) if blocks else "") + ("\n" if blocks else "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")

    @staticmethod
    def _render_block(s: Shortcut) -> str:
        return (
            f"{START}\n"
            f"id: {s.id}\n"
            f"name: {s.name}\n"
            f"photo: {'true' if s.photo else 'false'}\n"
            f"created: {s.created}\n"
            f"updated: {s.updated}\n"
            f"{BODY}\n\n"
            f"{s.prompt}\n"
            f"{END}\n"
        )
