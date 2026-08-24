#!/usr/bin/env python3
"""Import a conference program (papers_by_room.json) into Zack's Twin.

Normalizes the scraped `{room: [ {title, authors, abstract, date, url, domain,
award}, … ]}` shape into a FLAT list with the room folded in and the human date
string ("Tue, 14 Apr | 4:32 PM - 4:34 PM") parsed to ISO start/end — so the
conference-program skill can filter by a time window with a cheap query instead
of loading 4 MB into the agent's context.

Writes:
  <twin>/conferences/<slug>/program.json   normalized list (with abstracts)
  <twin>/conferences/<slug>/meta.json      {name, slug, dates, n_items, rooms, …}

Usage:
  import_conference_program.py <papers_by_room.json> <slug> [--name "CHI 2026"] \
      [--year 2026] [--twin ~/constellation/twin]

Times are stored at face value (as published in the program); we don't guess a
timezone. The agent plans against them directly + can show date_raw.
"""

from __future__ import annotations

import argparse
import json
import os
import re

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

# "Tue, 14 Apr | 4:32 PM - 4:34 PM"  →  day, mon, h1,m1,ap1, h2,m2,ap2
_DATE_RE = re.compile(
    r"[A-Za-z]{3},\s*(\d{1,2})\s+([A-Za-z]{3})\s*\|\s*"
    r"(\d{1,2}):(\d{2})\s*([AP]M)\s*-\s*(\d{1,2}):(\d{2})\s*([AP]M)")


def _to_24h(h: int, ap: str) -> int:
    h %= 12
    return h + 12 if ap.upper() == "PM" else h


def parse_date(s: str, year: int) -> tuple[str | None, str | None]:
    """'Tue, 14 Apr | 4:32 PM - 4:34 PM' → (ISO start, ISO end). ('',None) safe."""
    if not s:
        return None, None
    m = _DATE_RE.search(s)
    if not m:
        return None, None
    day, mon, h1, m1, ap1, h2, m2, ap2 = m.groups()
    mon_n = _MONTHS.get(mon.title())
    if not mon_n:
        return None, None
    sh, eh = _to_24h(int(h1), ap1), _to_24h(int(h2), ap2)
    start = f"{year:04d}-{mon_n:02d}-{int(day):02d}T{sh:02d}:{int(m1):02d}:00"
    end = f"{year:04d}-{mon_n:02d}-{int(day):02d}T{eh:02d}:{int(m2):02d}:00"
    return start, end


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("slug")
    ap.add_argument("--name", default=None)
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--twin", default="~/constellation/twin")
    a = ap.parse_args()

    raw = json.load(open(os.path.expanduser(a.source), encoding="utf-8"))
    out: list[dict] = []
    for room, lst in raw.items():
        for it in lst:
            start, end = parse_date(it.get("date", ""), a.year)
            entry = {
                "title": (it.get("title") or "").strip(),
                "authors": it.get("authors") or [],
                "room": room,
                "domain": (it.get("domain") or "").strip(),
                "start": start,
                "end": end,
                "date_raw": (it.get("date") or "").strip(),
                "url": (it.get("url") or "").strip(),
                "abstract": (it.get("abstract") or "").strip(),
            }
            if it.get("award"):
                entry["award"] = it["award"]
            out.append(entry)

    out.sort(key=lambda e: (e["start"] or "9999", e["room"]))
    starts = sorted(e["start"][:10] for e in out if e["start"])
    meta = {
        "name": a.name or a.slug,
        "slug": a.slug,
        "n_items": len(out),
        "n_rooms": len(raw),
        "rooms": sorted(raw.keys()),
        "dates": sorted(set(starts)),
        "n_timed": sum(1 for e in out if e["start"]),
        "source_file": os.path.basename(a.source),
        "note": "times are as-published in the program (no timezone normalization)",
    }

    dest = os.path.join(os.path.expanduser(a.twin), "conferences", a.slug)
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "program.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"wrote {dest}/program.json ({len(out)} items, {meta['n_timed']} timed)")
    print(f"dates: {meta['dates']}  rooms: {meta['n_rooms']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
