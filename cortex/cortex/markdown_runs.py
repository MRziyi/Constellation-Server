"""Markdown → styled-runs parser for the Glass HUD.

The Glass renders card bodies as a flat array of `{text, style}` runs, one
TextView per run. The server is responsible for splitting markdown into runs
so the Glass never sees raw `**`, `*`, or `` ` `` characters.

Supported inline styles (subset matching CXR-L TextView.textStyle):
  - normal       (default)
  - bold         (**x** / __x__)
  - italic       (*x* / _x_  — only when not adjacent to a word boundary that
                              would conflict with bold)
  - bold_italic  (***x*** / ___x___)
  - code         (`x`)
  - dim          (markers we set ourselves for `meta:` style sub-lines)

Block elements (headings, tables, images, lists) are flattened to plain text
with simple prefixes (e.g. `- item` → `• item`). The Glass HUD is text-only.

Output:
  [
    {"text": "Take a break.", "style": "bold"},
    {"text": " 30 min walk...", "style": "normal"},
    ...
  ]
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["to_runs", "Run"]


Run = dict[str, str]  # {"text": str, "style": str}


# Inline patterns, ordered so that the longest/most-specific match first.
# Each entry: (regex, captured-group-name, style).
_INLINE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ***bold-italic*** / ___bold-italic___ (must precede bold + italic)
    (re.compile(r"\*\*\*(.+?)\*\*\*"), "bold_italic"),
    (re.compile(r"___(.+?)___"),       "bold_italic"),
    # **bold** / __bold__
    (re.compile(r"\*\*(.+?)\*\*"), "bold"),
    (re.compile(r"__(.+?)__"),     "bold"),
    # *italic* / _italic_  — careful with single underscores in identifiers,
    # but for card bodies this is rare and acceptable to mis-parse.
    (re.compile(r"(?<!\w)\*(.+?)\*(?!\w)"), "italic"),
    (re.compile(r"(?<!\w)_(.+?)_(?!\w)"),   "italic"),
    # `code`
    (re.compile(r"`([^`]+?)`"), "code"),
]


def _block_to_text(line: str) -> str:
    """Normalize block-level markdown (headings / lists) to plain text the
    Glass can read. Keeps inline markers intact (they get parsed next)."""
    s = line.rstrip()
    # Headings: drop the # and trim.
    if s.startswith("#"):
        return s.lstrip("#").strip()
    # Bullets: - x  /  * x  → • x
    if re.match(r"^\s*[-*]\s+", s):
        return re.sub(r"^\s*[-*]\s+", "• ", s)
    # Numbered list: 1. x → keep (it's already readable)
    return s


def _parse_inline(text: str) -> list[Run]:
    """Walk the inline patterns left-to-right, splitting `text` into runs.

    Recursive: when a styled span is found, its inner text is parsed again
    so that ` `code` ` inside **bold** etc. work. For card bodies this is
    overkill; we keep it for correctness.
    """
    if not text:
        return []

    # Find the earliest match across all patterns.
    earliest: tuple[int, re.Match[str], str] | None = None
    for pat, style in _INLINE_PATTERNS:
        m = pat.search(text)
        if m is None:
            continue
        if earliest is None or m.start() < earliest[0]:
            earliest = (m.start(), m, style)

    if earliest is None:
        return [{"text": text, "style": "normal"}]

    start, m, style = earliest
    before = text[:start]
    after = text[m.end():]
    inner = m.group(1)

    runs: list[Run] = []
    if before:
        runs.extend(_parse_inline(before))
    # The inner span carries `style`. For bold/italic combos we keep nested
    # styles flat — outer wins for the whole span (an acceptable simplification
    # given monochrome-green has 3 levels of brightness, not unlimited combos).
    if style == "code":
        runs.append({"text": inner, "style": "code"})
    else:
        # Allow nested parsing for emphasis combos
        nested = _parse_inline(inner)
        for r in nested:
            # If nested run is already styled, prefer outer if outer is stronger
            r_style = r["style"]
            if r_style == "normal":
                r["style"] = style
            elif style == "bold" and r_style == "italic":
                r["style"] = "bold_italic"
            elif style == "italic" and r_style == "bold":
                r["style"] = "bold_italic"
            runs.append(r)
    if after:
        runs.extend(_parse_inline(after))
    return runs


def to_runs(markdown: str) -> list[Run]:
    """Top-level converter. Splits on lines, converts block-level prefixes,
    parses inline emphasis. Newlines are preserved as `\\n` characters inside
    `normal` runs (the Glass renders them via TextView line-breaks).
    """
    if not markdown:
        return []
    out: list[Run] = []
    lines = markdown.split("\n")
    for i, raw in enumerate(lines):
        block = _block_to_text(raw)
        runs = _parse_inline(block)
        out.extend(runs)
        if i < len(lines) - 1:
            # Carry the newline as a normal run (so the Glass renderer just
            # emits a line break inside the TextView).
            out.append({"text": "\n", "style": "normal"})

    # Merge adjacent runs with the same style — keeps the JSON compact.
    merged: list[Run] = []
    for r in out:
        if merged and merged[-1]["style"] == r["style"]:
            merged[-1]["text"] += r["text"]
        else:
            merged.append(dict(r))
    return merged


# ── tiny smoke test ────────────────────────────────────────────────────────
if __name__ == "__main__":
    samples = [
        "**Take a break.** 30 min walk, get water.",
        "Reminder · 9pm tonight\n\n**Take a break.** Then read.",
        "Sent to `mike@example.com`",
        "***Important***: this is _italic_ and **bold** and `code`.",
        "- first\n- second\n- third",
    ]
    import json
    for s in samples:
        print(f"\nIN:  {s!r}")
        print(f"OUT: {json.dumps(to_runs(s), ensure_ascii=False, indent=2)}")
