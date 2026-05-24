"""twin_query adapter — semantic Q&A over the Digital Twin.

Single action: `ask(question, max_snippets?, model?) → {answer, snippets}`

Implementation: grep across Twin → collect snippets → GPT synthesizes an answer using only
those snippets as ground truth. The exact RAG engine described in INTERFACE-CONTRACTS §5.1
for the MCP `query_twin` endpoint; same engine, exposed as a Cortex-callable tool here so
Router can ask "did Zack ever mention X?" mid-plan.

This is the only Tool Agent adapter that calls an LLM (the rest are pure local). Picks up
`OPENAI_API_KEY` from the same `.env` Cortex uses.

Side-effects: none (read + LLM call). Confirm: auto.

Cost note: each call ~ 1k-5k input tokens depending on grep result size + ~200 output.
Cheap enough for routine use; if budget gets tight, raise `max_snippets` floor.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any


TWIN_ROOT = Path.home() / "constellation" / "twin"
DEFAULT_MODEL = "gpt-5.2"


def _expand_question_to_keywords(question: str) -> list[str]:
    """Cheap keyword extraction: split on whitespace, strip punctuation, drop stopwords + short."""
    import re
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "do", "does", "did",
        "what", "who", "where", "when", "why", "how", "which",
        "i", "me", "my", "you", "your", "we", "our", "he", "she", "it", "they",
        "to", "of", "in", "on", "at", "by", "for", "with", "as", "from",
        "and", "or", "but", "if", "then", "than",
        "about", "into", "over", "have", "has", "had", "be", "been", "being",
    }
    tokens = re.findall(r"[\w一-鿿]+", question.lower())
    return [t for t in tokens if len(t) >= 2 and t not in stopwords][:12]


async def _which(name: str) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "which", name, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    out = out.decode().strip()
    return out or None


async def _ripgrep(keywords: list[str], max_matches: int = 30) -> list[dict[str, Any]]:
    """Search Twin for any of the keywords (case-insensitive). Uses rg if available, grep otherwise."""
    if not keywords:
        return []
    pattern = "|".join(keywords)
    if await _which("rg"):
        cmd = [
            "rg", "-i", "--max-count", str(max_matches), "--no-heading", "--line-number",
            "-C", "1", "--color=never",
            pattern, str(TWIN_ROOT),
        ]
    else:
        cmd = ["grep", "-r", "-i", "-n", "-E", "-C", "1", pattern, str(TWIN_ROOT)]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", errors="replace")
    snippets: list[dict[str, Any]] = []
    current_path: str | None = None
    buffer: list[str] = []

    for line in text.splitlines():
        if not line.strip():
            if buffer and current_path:
                snippets.append({"path": current_path, "text": "\n".join(buffer)})
            buffer = []
            current_path = None
            continue
        # rg format: path:line_no:content   OR   path-line_no-content (context)
        sep = None
        for s in (":", "-"):
            idx = line.find(s)
            if idx > 0 and line[idx + 1 : idx + 6].lstrip("0123456789") and line[idx + 1].isdigit():
                sep = s
                break
        if sep:
            parts = line.split(sep, 2)
            if len(parts) >= 3:
                path = parts[0]
                if current_path is None:
                    current_path = path
                buffer.append(parts[2])
        if len(snippets) >= max_matches:
            break
    if buffer and current_path:
        snippets.append({"path": current_path, "text": "\n".join(buffer)})

    # Dedupe by (path, first 100 chars)
    seen = set()
    deduped = []
    for s in snippets:
        key = (s["path"], s["text"][:100])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped[:max_matches]


async def _synthesize(question: str, snippets: list[dict[str, Any]], model: str) -> str:
    if not os.environ.get("OPENAI_API_KEY"):
        return "(twin_query: OPENAI_API_KEY not set; returning grep snippets only)"

    # Use the shared cache layer (cortex.llm_cache). Tool Agent imports it from the
    # cortex sibling package via sys.path manipulation, but the simpler path is to
    # let both venvs install diskcache and reach the same cache directory directly.
    # We inline the import so this adapter doesn't crash if the cache layer ever
    # moves; fall back to raw OpenAI client if unavailable.
    try:
        # cortex/cortex/llm_cache.py is importable when both venvs share the cache dir
        # at ~/constellation/cache/llm/. We add cortex/ to sys.path lazily.
        import sys
        cortex_pkg = Path(__file__).resolve().parents[3] / "cortex"
        if str(cortex_pkg) not in sys.path:
            sys.path.insert(0, str(cortex_pkg))
        from cortex.llm_cache import cached_chat_create  # type: ignore
        use_cache = True
    except Exception:
        use_cache = False

    snippet_block = "\n\n".join(
        f"--- {s['path']} ---\n{s['text']}" for s in snippets[:20]
    ) or "(no snippets found)"

    system = (
        "You are answering a question about Zack from his Digital Twin (markdown notes). "
        "ONLY use the snippets provided; if they don't contain the answer, say so directly. "
        "Be concise (1-3 sentences). Cite the source path(s) in [[brackets]] inline."
    )
    user = f"Question: {question}\n\nSnippets:\n{snippet_block}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    try:
        if use_cache:
            content = await cached_chat_create(
                model=model, messages=messages, purpose="twin_query"
            )
            return content.strip()
        else:
            from openai import AsyncOpenAI
            client = AsyncOpenAI()
            resp = await client.chat.completions.create(model=model, messages=messages)
            return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return f"(twin_query: LLM call failed: {e})"


class TwinQueryAdapter:
    name = "twin_query"

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        if action != "ask":
            raise ValueError(f"twin_query: unknown action '{action}' (only 'ask')")
        question = args.get("question") or args.get("q")
        if not question:
            raise ValueError("twin_query.ask: 'question' required")
        max_snippets = int(args.get("max_snippets", 20))
        model = args.get("model", DEFAULT_MODEL)

        keywords = _expand_question_to_keywords(question)
        snippets = await _ripgrep(keywords, max_matches=max_snippets)
        answer = await _synthesize(question, snippets, model)

        return {
            "question": question,
            "keywords": keywords,
            "answer": answer,
            "snippet_count": len(snippets),
            "snippets": snippets,
        }
