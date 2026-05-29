"""session_browser — UC2: voice-browse past Claude Code sessions by project.

Zack's use case:
  "List the recent sessions in <project> with their titles."  → list_project_sessions
  "Go into the gesture one / #2 — what was the last thing I said?"  → (title shown
       + last_user_msg already in the listing)
  "Continue that session — tell it to do X."  → resolve_pick + the agent resume
       path (`claude --resume <uuid>`), driven by server.py.

Design notes / cost:
- LISTING is a deterministic disk read (NO LLM): we tail each candidate jsonl for
  its first user line (title) + last user line (the "what did I say last"). Only
  the top-N most-recent jsonls for the matched project are parsed, so a project
  with 50 sessions still only opens ~5 files.
- Picking among the listed sessions is disambiguated by the existing
  session_router LLM, which sees only the compact index (title + last_user_msg),
  never the full transcripts.
- RESUME rehydrates CC's own context via `--resume <uuid>` (CC's window + prompt
  cache); Cortex never re-stuffs the transcript. So no "big context" blow-up.

The CC jsonl archive lives at ~/.claude/projects/<sanitised-working-dir>/<uuid>.jsonl
(the same buckets http.py's /api/cc-archive walks).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Voice triggers for "show me my sessions in <project>". EN + ZH. Kept narrow so
# it doesn't shadow ordinary asks; the project name is extracted separately.
_BROWSE_PATTERNS = [
    re.compile(
        r"\b(list|show|what|which|recent)\b.*\b(session|sessions|conversation|conversations|chat|chats|threads?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(列(一?下)?|看(一?下)?|有哪些|最近).*(session|会话|对话|聊天|线程)"),
]


def looks_session_browse(text: str) -> bool:
    """True iff the utterance is asking to browse past sessions."""
    if not text:
        return False
    return any(p.search(text) for p in _BROWSE_PATTERNS)


# Stopwords stripped when guessing the project name out of a browse utterance.
_BROWSE_STOPWORDS = {
    "list", "show", "me", "my", "the", "recent", "last", "few", "what", "which",
    "are", "is", "in", "of", "for", "project", "projects", "folder", "session",
    "sessions", "conversation", "conversations", "chat", "chats", "thread",
    "threads", "under", "from", "please", "go", "into",
}


def extract_project_query(text: str) -> str:
    """Pull the likely project name out of a browse utterance. We strip trigger
    stopwords; whatever distinctive token(s) remain are fuzzy-matched against the
    archive buckets (so 'list sessions in R08' → 'r08'). Chinese: drop the
    browse verbs/nouns, keep the rest."""
    if not text:
        return ""
    # Drop Chinese browse verbs/nouns so a CJK project token survives.
    zh = re.sub(r"(列(一?下)?|看(一?下)?|有哪些|最近(的|几次)?|会话|对话|聊天|线程|的|里(边|面)?|项目|文件夹|下)", " ", text)
    toks = [w for w in re.split(r"[\s,，。:：'\"]+", zh) if w]
    toks = [w for w in toks if w.lower() not in _BROWSE_STOPWORDS]
    return " ".join(toks).strip()


_PICK_NUM = re.compile(r"#\s*(\d+)|\bnumber\s+(\d+)|\bsession\s+(\d+)\b|第\s*([0-9一二两三四五六])\s*[个条]?", re.IGNORECASE)
_PICK_ORDINAL = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "last": -1,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
}
_ZH_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}


def parse_pick(text: str, n_sessions: int) -> tuple[int | None, str]:
    """Resolve a pick out of a follow-up utterance against a listing of
    `n_sessions`. Returns (1-based index | None, instruction-remainder).

    Examples (n=4):
      "continue #2: also count the lines" → (2, "also count the lines")
      "接着第二个跑，让它提交一版"          → (2, "让它提交一版")
      "open the last one"                  → (4, "")
    """
    if not text or n_sessions <= 0:
        return None, ""
    t = text.strip()
    idx: int | None = None

    m = _PICK_NUM.search(t)
    if m:
        g = next((x for x in m.groups() if x), None)
        if g:
            idx = _ZH_NUM.get(g, None) or (int(g) if g.isdigit() else None)
    if idx is None:
        low = t.lower()
        for word, val in _PICK_ORDINAL.items():
            if re.search(rf"\b{re.escape(word)}\b" if word.isascii() else re.escape(word), low):
                idx = n_sessions if val == -1 else val
                break

    if idx is None:
        return None, ""
    if idx == -1 or idx > n_sessions:
        idx = n_sessions
    if idx < 1:
        return None, ""

    # Instruction = text after a separator, else after the matched pick phrase.
    instruction = ""
    sep = re.split(r"[:：,，。]\s*", t, maxsplit=1)
    if len(sep) == 2 and sep[1].strip():
        instruction = sep[1].strip()
    elif m:
        tail = t[m.end():].strip(" ，,。.:：、")
        # drop a leading connective verb ("跑"/"continue"/"and …")
        tail = re.sub(r"^(跑|continue|and|then|去跑|让它|tell it to|让他)\s*", "", tail, flags=re.IGNORECASE).strip()
        instruction = tail
    return idx, instruction


def _bucket_to_path(bucket: str) -> str:
    """Best-effort un-sanitise a bucket dir name back to a path for DISPLAY only.
    Lossy (dashes in real names collapse), so we never use this to locate files —
    the bucket name itself is authoritative."""
    return "/" + bucket.lstrip("-").replace("-", "/")


def match_project_buckets(project_query: str) -> list[str]:
    """Return bucket dir names whose path contains the query (case-insensitive,
    most-recently-active first). Empty query → all buckets."""
    if not _PROJECTS_ROOT.exists():
        return []
    q = (project_query or "").strip().lower().replace(" ", "")
    buckets: list[tuple[float, str]] = []
    for d in _PROJECTS_ROOT.iterdir():
        if not d.is_dir():
            continue
        hay = d.name.lower().replace("-", "")
        if not q or q in hay:
            try:
                mtime = max((f.stat().st_mtime for f in d.glob("*.jsonl")), default=0.0)
            except OSError:
                mtime = 0.0
            buckets.append((mtime, d.name))
    buckets.sort(reverse=True)
    return [b for _, b in buckets]


def _distill_titles(jsonl: Path) -> dict[str, Any]:
    """One pass over a session jsonl → {title, last_user_msg, n_user_turns}.

    title    = first real user line (or an `ai-title`/`summary` entry if present)
    last_user_msg = the most recent real user message (skips tool_result + the
               XML-ish system reminders CC injects, which start with '<').
    """
    title: str | None = None
    first_user: str | None = None
    last_user: str | None = None
    n_user = 0

    def _user_text(ev: dict[str, Any]) -> str | None:
        msg = ev.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            txt = c
        elif isinstance(c, list):
            txt = None
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    txt = b.get("text")
                    break
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    return None  # tool result, not a human turn
        else:
            return None
        if not txt or not isinstance(txt, str):
            return None
        txt = txt.strip()
        # Skip CC's injected system-reminder / command blocks.
        if not txt or txt.startswith("<") or txt.startswith("Caveat:"):
            return None
        return txt

    try:
        with jsonl.open(errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                t = ev.get("type")
                if t in ("ai-title", "summary"):
                    title = ev.get("title") or ev.get("summary") or title
                elif t == "user":
                    u = _user_text(ev)
                    if u:
                        n_user += 1
                        if first_user is None:
                            first_user = u
                        last_user = u
    except OSError:
        pass

    disp_title = (title or first_user or "(untitled)").splitlines()[0][:80]
    return {
        "title": disp_title,
        "last_user_msg": (last_user or "").splitlines()[0][:160] if last_user else "",
        "n_user_turns": n_user,
    }


def list_project_sessions(project_query: str, limit: int = 5) -> dict[str, Any]:
    """List the most-recent CC sessions for the matched project.

    Returns {matched_bucket, working_dir, sessions: [{session_id, title,
    last_user_msg, n_user_turns, created_at, age_min}]}. Only the top `limit`
    most-recent jsonls are parsed (cheap)."""
    buckets = match_project_buckets(project_query)
    if not buckets:
        return {"matched_bucket": None, "working_dir": None, "sessions": [],
                "why": f"no project bucket matched '{project_query}'"}
    bucket = buckets[0]
    bdir = _PROJECTS_ROOT / bucket
    files = sorted(bdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    now = datetime.now(timezone.utc).timestamp()
    sessions: list[dict[str, Any]] = []
    for jsonl in files[:limit]:
        st = jsonl.stat()
        meta = _distill_titles(jsonl)
        sessions.append({
            "session_id": jsonl.stem,
            "title": meta["title"],
            "last_user_msg": meta["last_user_msg"],
            "n_user_turns": meta["n_user_turns"],
            "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            "age_min": int((now - st.st_mtime) / 60),
            "working_dir": _bucket_to_path(bucket),
            "bucket": bucket,
        })
    log.info("session_browser.listed", project=project_query, bucket=bucket,
             n=len(sessions))
    return {
        "matched_bucket": bucket,
        "working_dir": _bucket_to_path(bucket),
        "sessions": sessions,
    }


def resolve_session_jsonl(session_id: str) -> Path | None:
    """Find a session's jsonl across all buckets (for resume / last-msg lookup)."""
    if not _PROJECTS_ROOT.exists():
        return None
    for d in _PROJECTS_ROOT.iterdir():
        cand = d / f"{session_id}.jsonl"
        if cand.exists():
            return cand
    return None


def working_dir_for_session(session_id: str) -> str | None:
    """The real on-disk working dir for a session = its bucket dir's true path.
    We resolve this from the jsonl's own `cwd` field (authoritative, unlike the
    lossy bucket-name un-sanitise) so `--resume` runs in the right place."""
    jsonl = resolve_session_jsonl(session_id)
    if not jsonl:
        return None
    try:
        with jsonl.open(errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                cwd = ev.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return None
