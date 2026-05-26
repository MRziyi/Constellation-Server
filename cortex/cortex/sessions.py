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

import contextvars
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import structlog

from . import ids


# P0.3 — context-scoped HUD session id. LLM calls fired inside a turn
# pick this up via _emit so cost/latency can be attributed to the right
# session for the /sessions cost roll-up. Async-safe via ContextVar.
current_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_session_id", default=None,
)


@contextmanager
def in_session(session_id: str | None):
    """Run a block with `current_session_id` set. LLM calls fired inside
    will be attributed to this session."""
    tok = current_session_id.set(session_id)
    try:
        yield
    finally:
        current_session_id.reset(tok)

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

    # P2.1 — sessions older than this are tagged 'archived' in the list
    # output. Default 7 days; the source of truth (JSONL files) is never
    # touched — archival is a derived label so the user can filter the UI.
    ARCHIVE_AFTER_DAYS = 7

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """Return index entries (sorted by last_activity DESC).

        The index is append-only and may contain multiple records per
        session (each turn writes an update). We collapse to the latest
        per session_id at read time — simpler than rewriting the file.

        P2.1: every entry is annotated with a derived `archived: bool`
        (last_activity older than ARCHIVE_AFTER_DAYS). `status` filter:
          - None / "all" → everything
          - "active"     → non-killed AND not archived
          - "archived"   → archived (including killed)
          - "killed"     → killed only (regardless of age)
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
        # Annotate `archived`
        now = datetime.now(timezone.utc)
        cutoff = now - __import__("datetime").timedelta(days=self.ARCHIVE_AFTER_DAYS)
        for r in out:
            la = r.get("last_activity") or ""
            try:
                la_dt = datetime.fromisoformat(la)
            except Exception:
                la_dt = now
            r["archived"] = la_dt < cutoff
        # Apply filter
        if status == "active":
            out = [r for r in out if r.get("status") != "killed" and not r.get("archived")]
        elif status == "archived":
            out = [r for r in out if r.get("archived")]
        elif status == "killed":
            out = [r for r in out if r.get("status") == "killed"]
        # else "all" / None → no filter
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
        # P0.3 — added card_surfaced + agent_completed so cost/latency totals
        # refresh as soon as each turn lands its card; llm_call also bumps so
        # the running prompt_chars/latency are queryable mid-turn.
        if kind in (
            "turn_start", "decision", "turn_complete", "session_killed",
            "title_set", "card_surfaced", "agent_completed", "llm_call",
        ):
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
        # selector/router calls captured via the cortex.main LLM observer.
        # (LLM observer in main.py forwards to SessionStore.append(kind=
        # "llm_call", ...) when current_session_id ContextVar is set.)
        llm_calls = [r for r in recs if r.get("kind") == "llm_call"]
        llm_total_chars = sum(
            int(r.get("prompt_chars", 0) or 0) + int(r.get("completion_chars", 0) or 0)
            for r in llm_calls
        )
        llm_latency_ms = sum(int(r.get("latency_ms", 0) or 0) for r in llm_calls)
        llm_by_purpose: dict[str, int] = {}
        for r in llm_calls:
            p = str(r.get("purpose") or "?")
            llm_by_purpose[p] = llm_by_purpose.get(p, 0) + 1
        agent_dispatches = sum(1 for r in recs if r.get("kind") == "agent_dispatch")

        # Per-turn wall-clock: from each turn_start to its next turn_start
        # (or last record). Useful for "how long did this conversation feel?"
        turn_wallclocks_ms: list[int] = []
        cursor_ts: str | None = None
        for r in recs:
            if r.get("kind") == "turn_start":
                if cursor_ts:
                    pass  # close previous; computed below
                cursor_ts = r.get("ts")
            # find any subsequent record's ts to close a turn
        # Simpler O(n): pair turn_start ts → (next turn_start ts | last ts)
        starts = [r.get("ts") for r in recs if r.get("kind") == "turn_start" and r.get("ts")]
        last_ts = recs[-1].get("ts") if recs else None
        for i, s in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else last_ts
            if s and end:
                try:
                    d = datetime.fromisoformat(end) - datetime.fromisoformat(s)
                    turn_wallclocks_ms.append(int(d.total_seconds() * 1000))
                except Exception:
                    pass
        total_wallclock_ms = sum(turn_wallclocks_ms)
        # Agent tool uses across all agent_completed records
        n_tool_uses = sum(
            int(r.get("n_tool_uses", 0) or 0)
            for r in recs if r.get("kind") == "agent_completed"
        )

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
            "llm_latency_ms": llm_latency_ms,
            "llm_by_purpose": llm_by_purpose,
            "n_tool_uses": n_tool_uses,
            "total_wallclock_ms": total_wallclock_ms,
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
