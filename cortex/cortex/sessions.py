"""HUD session model — first-class conversation threads.

Each user_invoke either CREATES a new session (no session_id in payload)
or EXTENDS an existing one. A session = ordered list of turns. A turn =
one ask + its classifier decision + agent dispatch + cards + Zack's
decisions + executed actions + final receipt.

Persistence: append-only JSONL at
  ~/constellation/twin/_system/sessions/<session_id>.jsonl
plus a master index
  ~/constellation/twin/_system/sessions/_index.jsonl
of (session_id, title, created_at, last_activity, turn_count, etc.) for
the Sessions UI list view.

Design notes:
- One file per session keeps load cost proportional to a single session,
  not the whole archive. The _index.jsonl is the cheap summary stream.
- Records are append-only. The Sessions UI computes derived stats by
  scanning the file. No DB — same Twin-as-markdown discipline.
- Session "title" is derived from the first ask's text (truncated). User
  may eventually rename via /api/sessions/{id} PATCH.
- Token / cost stats: aggregated from llm.api_call observer records
  (each call carries purpose + prompt_chars / completion_chars). Agent
  path tokens not tracked (subscription, not API).

Public surface:
- SessionStore: holds the in-memory index + file IO.
- SessionStore.start_turn(session_id?, ask_text, has_image) → session_id
- SessionStore.append(session_id, record) — generic event append
- SessionStore.set_title(session_id, title)
- SessionStore.list() → list of index entries
- SessionStore.read(session_id) → all records
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import structlog

from . import ids

log = structlog.get_logger(__name__)


_SESSIONS_SUBDIR = "_system/sessions"
_INDEX_FILE = "_index.jsonl"
_TITLE_MAX = 80


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derive_title(ask_text: str) -> str:
    t = (ask_text or "").strip().splitlines()[0] if ask_text else ""
    if len(t) > _TITLE_MAX:
        t = t[: _TITLE_MAX - 1] + "…"
    return t or "(no title)"


class SessionStore:
    """File-backed session log.

    Thread-/task-safety: all writes use append mode + file-system locking
    semantics are sufficient for our single-process server. If we ever
    multi-process, swap to a real lock around the index file.
    """

    def __init__(self, twin_root: Path | str) -> None:
        self.root = Path(str(twin_root))
        self.sessions_dir = self.root / _SESSIONS_SUBDIR
        self.index_path = self.sessions_dir / _INDEX_FILE
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    # ── Index ─────────────────────────────────────────────────────────

    def list(self) -> list[dict[str, Any]]:
        """Return ALL index entries (sorted by last_activity DESC).

        The index is append-only and may contain multiple records per
        session (each turn writes an update). We collapse to the latest
        per session_id at read time — simpler than rewriting the file.
        """
        if not self.index_path.exists():
            return []
        latest: dict[str, dict[str, Any]] = {}
        try:
            with self.index_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = rec.get("session_id")
                    if not sid:
                        continue
                    # Last record for this session wins
                    latest[sid] = rec
        except OSError as e:
            log.warning("sessions.index_read_failed", error=str(e))
            return []
        out = list(latest.values())
        out.sort(key=lambda r: r.get("last_activity") or "", reverse=True)
        return out

    def _append_index(self, rec: dict[str, Any]) -> None:
        try:
            with self.index_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("sessions.index_append_failed", error=str(e))

    # ── Per-session file ──────────────────────────────────────────────

    def _file_for(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def append(self, session_id: str, kind: str, **fields: Any) -> None:
        """Append one record to a session's log AND bump the index entry's
        last_activity / turn-count if relevant.

        Common kinds:
          turn_start | classifier | agent_dispatch | card_surfaced |
          decision  | action_executed | receipt | turn_complete |
          title_set | session_killed
        """
        ts = _now_iso()
        rec = {"ts": ts, "kind": kind, **fields}
        try:
            with self._file_for(session_id).open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("sessions.session_append_failed", session_id=session_id, error=str(e))
        # For events that mark a meaningful update, bump the index.
        if kind in ("turn_start", "decision", "turn_complete", "session_killed", "title_set"):
            self._bump_index(session_id, ts=ts)

    def _bump_index(self, session_id: str, *, ts: str | None = None) -> None:
        """Recompute and append a fresh index entry for this session."""
        ts = ts or _now_iso()
        recs = list(self.read(session_id))
        # Compute summary from session file.
        title = next(
            (r.get("title") for r in recs if r.get("kind") == "title_set" and r.get("title")),
            None,
        )
        if not title:
            first_ask = next(
                (r.get("ask_text") for r in recs if r.get("kind") == "turn_start"),
                "",
            )
            title = _derive_title(first_ask or "")
        created_at = recs[0].get("ts") if recs else ts
        turn_count = sum(1 for r in recs if r.get("kind") == "turn_start")
        decision_counts: dict[str, int] = {"approve": 0, "modify": 0, "kill": 0}
        for r in recs:
            if r.get("kind") == "decision":
                dk = r.get("decision_kind")
                if dk in decision_counts:
                    decision_counts[dk] += 1
        killed = any(r.get("kind") == "session_killed" for r in recs)
        # LLM cost roll-up: sum prompt+completion chars across classifier/
        # selector/router calls captured via SessionStore.append(... kind=
        # "llm_call", ...). May be 0 if no LLM observer plumbed yet.
        llm_calls = [r for r in recs if r.get("kind") == "llm_call"]
        llm_total_chars = sum(
            int(r.get("prompt_chars", 0) or 0) + int(r.get("completion_chars", 0) or 0)
            for r in llm_calls
        )
        agent_dispatches = sum(1 for r in recs if r.get("kind") == "agent_dispatch")

        self._append_index({
            "session_id": session_id,
            "title": title,
            "created_at": created_at,
            "last_activity": ts,
            "turn_count": turn_count,
            "agent_dispatch_count": agent_dispatches,
            "decision_counts": decision_counts,
            "llm_call_count": len(llm_calls),
            "llm_total_chars": llm_total_chars,
            "status": "killed" if killed else "active",
        })

    def read(self, session_id: str) -> Iterable[dict[str, Any]]:
        p = self._file_for(session_id)
        if not p.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            log.warning("sessions.session_read_failed", session_id=session_id, error=str(e))
        return out

    # ── Convenience for the server ────────────────────────────────────

    def start_turn(
        self,
        *,
        existing_session_id: str | None,
        event_id: str,
        ask_text: str,
        has_image: bool,
    ) -> str:
        """Start a new turn. Returns the session_id to use (newly created
        or the one passed in).

        If existing_session_id is provided and the file exists, the turn
        is appended to that session. Otherwise a fresh session is created.
        """
        if existing_session_id and self._file_for(existing_session_id).exists():
            sid = existing_session_id
        else:
            sid = ids.session_id()
        self.append(
            sid, "turn_start",
            event_id=event_id, ask_text=ask_text, has_image=has_image,
        )
        return sid

    def set_title(self, session_id: str, title: str) -> None:
        if not (title or "").strip():
            return
        self.append(session_id, "title_set", title=title.strip()[:_TITLE_MAX])
