"""Twin reader / writer + CHANGELOG appender.

Twin layout per DATA-MODEL.md §3. Write protocol per §10. CHANGELOG format per §12.

v1 minimal: read-anything, write-with-mtime-check, append-only CHANGELOG.
v0.5 adds:
  - build_toc()                : parse _system/TOC.md + auto-discover any twin
                                 files not yet curated; returns the
                                 (path, description) table the selector pass
                                 of the Router shows to the LLM.
  - assemble_context_pack(paths): now takes an explicit path list. Callers
                                 must pass the paths the selector picked.
                                 Default (no args) = identity.md only — a
                                 safe minimal fallback if selector failed.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path


class Twin:
    """Twin reader / writer rooted at a given directory (default ~/constellation/twin/)."""

    def __init__(self, root: Path | str | None = None):
        if root is None:
            root = Path.home() / "constellation" / "twin"
        self.root = Path(root)
        if not self.root.exists():
            raise FileNotFoundError(
                f"Twin not found at {self.root}. Run: "
                f"cp -r ~/Code/Projects/Constellation/twin-seed {self.root}"
            )
        self._read_mtimes: dict[Path, float] = {}

    # ── Reads ──

    def read(self, relpath: str) -> str:
        """Read a file relative to Twin root. Tracks mtime for later conflict check."""
        p = self.root / relpath
        content = p.read_text(encoding="utf-8")
        self._read_mtimes[p] = p.stat().st_mtime
        return content

    def exists(self, relpath: str) -> bool:
        return (self.root / relpath).exists()

    # ── Writes ──

    def append(self, relpath: str, content: str) -> None:
        """Append to a file. Creates parent dirs as needed. Always safe."""
        p = self.root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)

    def write_new(self, relpath: str, content: str) -> None:
        """Create a new file. Raises if it exists."""
        p = self.root / relpath
        if p.exists():
            raise FileExistsError(f"Refusing to overwrite existing {p}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def overwrite_if_no_conflict(self, relpath: str, content: str) -> bool:
        """Overwrite a file but only if mtime hasn't moved since last read.
        Returns True if written, False if conflict detected.
        Per skills/twin-write-policy.md.
        """
        p = self.root / relpath
        prior = self._read_mtimes.get(p)
        current = p.stat().st_mtime if p.exists() else None
        if prior is not None and current is not None and current > prior + 5:
            # 5 s grace per skills/twin-write-policy.md
            return False
        p.write_text(content, encoding="utf-8")
        self._read_mtimes[p] = p.stat().st_mtime
        return True

    # ── CHANGELOG ──

    def changelog_append(self, summary: str, src: str, details: list[str] | None = None) -> None:
        """Append a CHANGELOG entry. Format per DATA-MODEL.md §12.

        summary: short label, e.g. "email reply to Jane"
        src: e.g. "evt_abc123" or "pulse_def456"
        details: optional bullet list of field-level changes
        """
        now = datetime.now(timezone.utc)
        date_h = now.strftime("## %Y-%m-%d")
        time_str = now.strftime("%H:%M")

        log_path = self.root / "CHANGELOG.md"
        log_path.touch(exist_ok=True)
        prior = log_path.read_text(encoding="utf-8") if log_path.exists() else ""

        # Add date header if today's date isn't there yet
        entry_lines = [f"\n### {time_str} — {summary} [src:{src}]"]
        if details:
            for d in details:
                entry_lines.append(f"- {d}")
        entry_lines.append("")  # blank line spacer

        if date_h not in prior:
            entry_lines.insert(0, f"\n{date_h}\n")

        with log_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(entry_lines))

    # ── Context pack assembly (v0.5 — explicit paths) ──

    def assemble_context_pack(self, paths: list[str] | None = None) -> dict[str, str]:
        """Return {relpath: content} for the Twin slices the planner needs.

        v0.5 behaviour:
          - paths=None (default): identity.md only — safe minimal fallback
                                  used when the selector pass returned nothing
                                  parseable.
          - paths=[...]:          read exactly those files (skip ones that
                                  don't exist; never escapes Twin root).

        v0.4 used to eager-load identity + all skills + all people/core; that
        cost ~6.5K tokens per call regardless of relevance. The selector now
        picks the relevant subset (see router.select_twin_paths).
        """
        if paths is None:
            paths = ["identity.md"]
        pack: dict[str, str] = {}
        root_resolved = self.root.resolve()
        for rel in paths:
            p = (self.root / rel).resolve()
            try:
                p.relative_to(root_resolved)  # path-escape guard
            except ValueError:
                continue
            if p.is_file():
                pack[rel] = p.read_text(encoding="utf-8")
        return pack

    # ── TOC (the Anthropic-Skill-style hook the selector pass uses) ──

    _toc_cache: tuple[float, list[tuple[str, str]]] | None = None

    def build_toc(self) -> list[tuple[str, str]]:
        """Return a flat list of (path, description) the selector pass shows.

        Hot-reloads on _system/TOC.md mtime change. Merges:
          1. Curated table rows in _system/TOC.md
          2. Auto-discovered .md files in `skills/`, `people/core/`,
             `people/encounters/`, `projects/`, `commitments/`, `interests/`
             that don't already appear in (1).

        Excludes (by design): receipts/*, CHANGELOG.md, README.md, _system/*
        themselves. These are transient log / meta files, not policy context.
        """
        toc_path = self.root / "_system" / "TOC.md"
        toc_mtime = toc_path.stat().st_mtime if toc_path.exists() else 0.0

        # Cache invalidates if TOC mtime moves; auto-discovery is cheap (a few
        # dozen file stats) so we just re-do it every call for now.
        # (Could cache against root mtime too if profiling demands.)
        if self._toc_cache and self._toc_cache[0] == toc_mtime:
            curated = self._toc_cache[1]
        else:
            curated = self._parse_curated_toc(toc_path) if toc_path.exists() else []
            self.__class__._toc_cache = (toc_mtime, curated)

        curated_paths = {p for p, _ in curated}
        extras = self._auto_discover(curated_paths)
        return curated + extras

    @staticmethod
    def _parse_curated_toc(toc_path: Path) -> list[tuple[str, str]]:
        """Extract (path, description) from the markdown tables in _system/TOC.md.

        Accepts rows like `| skills/X.md | one-line desc | 2026-... |`.
        Ignores header / separator / non-row text.
        """
        entries: list[tuple[str, str]] = []
        for line in toc_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not (s.startswith("|") and s.endswith("|")):
                continue
            parts = [c.strip() for c in s.strip("|").split("|")]
            if len(parts) < 2:
                continue
            path, desc = parts[0], parts[1]
            if not path or not desc:
                continue
            if path.lower() == "path" or set(path) <= {"-", ":"}:
                continue  # header or separator
            if path.startswith("receipts/") or path in ("CHANGELOG.md", "README.md"):
                continue
            entries.append((path, desc))
        return entries

    # Directories where files become eligible context for the planner.
    _DISCOVER_DIRS = (
        "skills", "people/core", "people/encounters",
        "projects", "commitments", "interests",
    )

    def _auto_discover(self, already_curated: set[str]) -> list[tuple[str, str]]:
        """Find .md files in twin dirs that aren't yet in the curated TOC."""
        out: list[tuple[str, str]] = []
        for sub in self._DISCOVER_DIRS:
            d = self.root / sub
            if not d.is_dir():
                continue
            for f in sorted(d.rglob("*.md")):
                if f.name.lower() == "readme.md":
                    continue
                rel = str(f.relative_to(self.root))
                if rel in already_curated:
                    continue
                desc = self._describe_from_frontmatter(f) or f.stem.replace("-", " ")
                out.append((rel, desc))
        return out

    @staticmethod
    def _describe_from_frontmatter(p: Path) -> str | None:
        """Best-effort one-liner from a Twin file's frontmatter."""
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return None
        fm = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not fm:
            return None
        meta: dict[str, str] = {}
        for line in fm.group(1).splitlines():
            m = re.match(r"^([\w_]+)\s*:\s*(.+?)\s*$", line)
            if m:
                meta[m.group(1)] = m.group(2).strip()
        if "description" in meta:
            return meta["description"].strip("'\"")
        # People-style: synthesise from relation / affiliation / preferred_contact
        if meta.get("type") == "person":
            bits = []
            if "relation" in meta:    bits.append(meta["relation"])
            if "affiliation" in meta: bits.append(f"at {meta['affiliation']}")
            if "preferred_contact" in meta:
                bits.append(f"prefers {meta['preferred_contact']}")
            if bits:
                return ", ".join(bits)
        return None

    def toc_as_table(self) -> str:
        """Render build_toc() output as a fixed-width table for the selector prompt."""
        entries = self.build_toc()
        if not entries:
            return "(Twin is empty)"
        path_w = max(len(p) for p, _ in entries)
        path_w = min(path_w, 40)  # cap so a freak path doesn't blow out the column
        lines = []
        for p, d in entries:
            p_disp = p if len(p) <= path_w else p[: path_w - 1] + "…"
            lines.append(f"{p_disp:<{path_w}}  {d}")
        return "\n".join(lines)

    # ── Receipts ──

    def receipt_append(self, body: str) -> None:
        """Append today's receipt entry to receipts/{date}.md."""
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rel = f"receipts/{date}.md"
        if not self.exists(rel):
            header = (
                "---\n"
                f"type: receipt\n"
                f"created: {datetime.now(timezone.utc).isoformat()}\n"
                f"date: {date}\n"
                "share: none\n"
                "confidence: 1.0\n"
                "---\n\n"
                f"# Receipts — {date}\n\n"
            )
            self.write_new(rel, header)
        self.append(rel, body + "\n")
