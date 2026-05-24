"""Twin reader / writer + CHANGELOG appender.

Twin layout per DATA-MODEL.md §3. Write protocol per §10. CHANGELOG format per §12.

v1 minimal: read-anything, write-with-mtime-check, append-only CHANGELOG.
No git, no snapshots, no TOC auto-rebuild (Phase 7 adds those).
"""

from __future__ import annotations

import os
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

    # ── Context pack assembly ──

    def assemble_context_pack(self) -> dict[str, str]:
        """Return {relpath: content} for Twin slices the Router should always see.

        Phase 2 Slice B/C version:
          - identity.md (always)
          - skills/*.md (all of them; small enough)
          - people/core/*.md (all of them; one file per core contact, small + high value)

        Phase 7+ will switch to two-pass: GPT reads _system/TOC.md first and picks
        which paths to load (per DATA-MODEL.md §11). Until then, eager-load.
        """
        pack: dict[str, str] = {}
        identity = self.root / "identity.md"
        if identity.exists():
            pack["identity.md"] = identity.read_text(encoding="utf-8")
        skills_dir = self.root / "skills"
        if skills_dir.is_dir():
            for p in sorted(skills_dir.glob("*.md")):
                if p.name == "README.md":
                    continue
                pack[f"skills/{p.name}"] = p.read_text(encoding="utf-8")
        people_core_dir = self.root / "people" / "core"
        if people_core_dir.is_dir():
            for p in sorted(people_core_dir.glob("*.md")):
                pack[f"people/core/{p.name}"] = p.read_text(encoding="utf-8")
        return pack

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
