"""LLM cache + retry + JSON parsing — single chokepoint for all LLM calls.

Modelled on Chrono's `server/utilities/llm_cache.py` but Constellation-shaped:
- async-only (the rest of cortex is asyncio)
- diskcache stored at ~/constellation/cache/llm/ (persists across daemon restart;
  outside repo; outside Twin)
- 3-retry with exponential backoff on transient errors (network / 5xx / 429)
- telemetry observer hook for session-level logging
- `parse_json_response()` handles ```json fences + json_repair recovery

Cache key = md5(provider + model + messages). Per Zack: different tasks have
different context, won't collide with stale cached outputs.

In tests / repeated dev iterations cache is a huge win. In production it also
shields against network blips (we still pay the retry cost on cache miss but at
least subsequent identical retries land instantly).

Usage:
    from cortex.llm_cache import cached_chat_create, parse_json_response

    raw = await cached_chat_create(
        model="gpt-5.2",
        messages=[{"role": "system", ...}, {"role": "user", ...}],
        purpose="router",   # for telemetry only
    )
    plan = parse_json_response(raw)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import structlog
from diskcache import Cache
from json_repair import repair_json
from openai import AsyncOpenAI

log = structlog.get_logger(__name__)


# ── cache location ──────────────────────────────────────────────────────────

_CACHE_DIR = Path.home() / "constellation" / "cache" / "llm"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
cache = Cache(str(_CACHE_DIR))


# ── telemetry hook (caller registers; we never crash on observer failure) ──

_CALL_OBSERVER: Callable[[dict[str, Any]], None] | None = None


def set_call_observer(fn: Callable[[dict[str, Any]], None] | None) -> None:
    global _CALL_OBSERVER
    _CALL_OBSERVER = fn


def _emit(**info: Any) -> None:
    if _CALL_OBSERVER is None:
        return
    # P0.3 — attribute this LLM call to the HUD session in scope (if any).
    # Imported lazily to avoid a circular import (sessions imports llm_cache
    # is not the case, but keeping the dependency light just to be safe).
    try:
        from .sessions import current_session_id as _csid
        sid = _csid.get()
        if sid and "session_id" not in info:
            info["session_id"] = sid
    except Exception:
        pass
    try:
        _CALL_OBSERVER(info)
    except Exception as e:
        log.warning("call_observer.failed", error=str(e))


def _msgs_chars(messages: list[dict[str, Any]]) -> int:
    try:
        return sum(len(str(m.get("content", ""))) for m in messages)
    except Exception:
        return 0


# ── retry helpers ───────────────────────────────────────────────────────────

class _RetryableLLMError(Exception):
    pass


def _is_retryable(exc: Exception) -> bool:
    """True if the exception looks transient (network / 5xx / 429)."""
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    # OpenAI SDK names: APIConnectionError, APITimeoutError, RateLimitError,
    # InternalServerError. We don't want to import them by name (sdk version churn);
    # match on substrings instead.
    transient_signals = (
        "connection", "timeout", "rate", "ratelimit", "internal", "503", "502",
        "504", "500", "overloaded", "service unavailable", "temporarily",
    )
    return any(s in name or s in msg for s in transient_signals)


# ── endpoint routing (OpenAI vs Groq, both OpenAI-compatible) ────────────────

# Groq serves an OpenAI-compatible API; we use it for the small fast models
# (e.g. the classifier on `openai/gpt-oss-120b`) so a trivial classify doesn't
# pay GPT-5.2's ~3s latency. Detection is by model name (a Groq-only substring),
# so callers don't have to thread a provider through.
GROQ_BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
_GROQ_MODEL_MARKERS = ("gpt-oss", "meta-llama", "llama-4", "llama-3.3", "llama-3.1",
                       "moonshotai/", "groq/")


def _resolve_endpoint(model: str) -> tuple[str | None, str]:
    """Map a model name → (base_url, api_key). Groq-hosted models route to Groq's
    OpenAI-compatible endpoint with GROQ_API_KEY; everything else to OpenAI
    (base_url None = the SDK default). Raises if the needed key is unset."""
    m = model.lower()
    if any(mk in m for mk in _GROQ_MODEL_MARKERS):
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            raise ValueError(
                f"GROQ_API_KEY not set (required for Groq model '{model}'); "
                f"add it to tool-agent/.env"
            )
        return GROQ_BASE_URL, key
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY not set")
    return None, key


# ── persistent (warm) clients ────────────────────────────────────────────────
# Reuse ONE AsyncOpenAI per endpoint so the TLS/HTTP keep-alive connection stays
# warm across calls. A fresh client per call (the old code) paid a cold TLS
# handshake to the provider EVERY time — that was most of the classifier's
# cold-vs-warm gap (~2.4s cold vs ~1.0s warm on Groq). Server perf isn't a
# concern (Zack 2026-06-02), so a long keepalive keeps the socket hot between
# sparse turns. Reset on key change (so a rotated GROQ_API_KEY is picked up).
_CLIENTS: dict[str, tuple[str, AsyncOpenAI]] = {}


def _client_for(base_url: str | None, api_key: str) -> AsyncOpenAI:
    cache_key = base_url or "openai"
    cached = _CLIENTS.get(cache_key)
    if cached is not None and cached[0] == api_key:
        return cached[1]
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=600.0),
        timeout=httpx.Timeout(60.0, connect=10.0),
    )
    client = (AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
              if base_url else AsyncOpenAI(api_key=api_key, http_client=http_client))
    _CLIENTS[cache_key] = (api_key, client)
    return client


async def prewarm_model(model: str) -> None:
    """Open the endpoint's TLS/keep-alive connection at startup so the FIRST real
    call isn't cold. Best-effort, no inference (just lists models). Called from
    cortex.main alongside the whisper/face prewarms."""
    try:
        base_url, api_key = _resolve_endpoint(model)
        client = _client_for(base_url, api_key)
        await client.models.list()
        log.info("llm.prewarm.done", model=model, endpoint=base_url or "openai")
    except Exception as e:
        log.warning("llm.prewarm.failed", model=model, error=str(e))


# ── main entry ──────────────────────────────────────────────────────────────

async def cached_chat_create(
    model: str,
    messages: list[dict[str, Any]],
    *,
    provider: str = "openai",
    purpose: str = "chat",
    response_format: dict[str, Any] | None = None,
    temperature: float | None = None,
    max_retries: int = 3,
    backoff_base_s: float = 1.0,
) -> str:
    """Async, cached chat completion.

    Returns the raw assistant message content string. Caller parses if needed.

    Caching: keyed on (provider, model, messages, response_format). Same inputs
    → identical cached output. `purpose` is telemetry-only and NOT in the key.

    Retries: up to `max_retries` on transient errors; exponential backoff
    (1s, 2s, 4s by default). Non-transient (4xx other than 429) raise immediately.

    response_format: pass `{"type": "json_object"}` to force OpenAI JSON mode.
    Leave None to allow reasoning prose + JSON code fence (parse_json_response
    extracts the fence). For models that support reasoning natively, None is
    usually better — Router can think before committing to a plan.
    """
    provider = provider.lower()
    prompt_chars = _msgs_chars(messages)

    key_payload = {
        "provider": provider,
        "model": model,
        "messages": messages,
        "response_format": response_format,
    }
    cache_key = hashlib.md5(
        json.dumps(key_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    # 1. cache lookup
    if cache_key in cache:
        cached_res: str = cache[cache_key]
        log.info("llm.cache_hit", key=cache_key[:10], model=model, purpose=purpose,
                 prompt_chars=prompt_chars, completion_chars=len(cached_res or ""))
        _emit(model=model, provider=provider, purpose=purpose, cache_hit=True,
              latency_ms=0, prompt_chars=prompt_chars,
              completion_chars=len(cached_res or ""),
              messages=messages, response=cached_res, cache_key=cache_key)
        return cached_res

    # 2. live call with retry
    if provider == "openai":
        base_url, api_key = _resolve_endpoint(model)
        client = _client_for(base_url, api_key)   # persistent, warm keep-alive
    else:
        raise ValueError(f"unsupported provider '{provider}' (extend cached_chat_create when needed)")

    last_exc: Exception | None = None
    t0 = time.monotonic()
    for attempt in range(max_retries):
        try:
            kwargs: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
            if response_format is not None:
                kwargs["response_format"] = response_format
            if temperature is not None:
                kwargs["temperature"] = temperature
            resp = await client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            latency_ms = int(round((time.monotonic() - t0) * 1000))
            log.info("llm.api_call", key=cache_key[:10], model=model, purpose=purpose,
                     attempt=attempt + 1, latency_ms=latency_ms,
                     prompt_chars=prompt_chars, completion_chars=len(content))
            _emit(model=model, provider=provider, purpose=purpose, cache_hit=False,
                  latency_ms=latency_ms, prompt_chars=prompt_chars,
                  completion_chars=len(content), attempt=attempt + 1,
                  messages=messages, response=content, cache_key=cache_key)
            # store in cache
            cache[cache_key] = content
            return content
        except Exception as e:
            last_exc = e
            if not _is_retryable(e) or attempt == max_retries - 1:
                log.error("llm.api_failed", key=cache_key[:10], model=model, purpose=purpose,
                          attempt=attempt + 1, error=str(e), error_type=type(e).__name__)
                _emit(model=model, provider=provider, purpose=purpose, cache_hit=False,
                      latency_ms=int(round((time.monotonic() - t0) * 1000)),
                      prompt_chars=prompt_chars, completion_chars=0,
                      attempt=attempt + 1, error=str(e),
                      messages=messages, response=None, cache_key=cache_key)
                raise
            sleep_s = backoff_base_s * (2 ** attempt)
            log.warning("llm.retry", key=cache_key[:10], model=model, purpose=purpose,
                        attempt=attempt + 1, sleep_s=sleep_s, error=str(e))
            await asyncio.sleep(sleep_s)
    # unreachable but appease type checker
    raise last_exc or RuntimeError("cached_chat_create: exhausted retries with no exception")


# ── JSON response parsing ────────────────────────────────────────────────────

def parse_json_response(content: str) -> Any:
    """Parse a JSON object out of an LLM response.

    LLMs occasionally emit JSON wrapped in ```json fences, with surrounding prose
    (e.g. reasoning preamble), or with subtle syntax errors. Try strict parsing,
    then fall back to json_repair so a single malformed character doesn't blow
    up the whole call.
    """
    if content is None or not content.strip():
        raise ValueError("empty LLM response")

    # Prefer ```json ... ``` fence; fall back to any ``` fence; then raw content.
    fence = re.search(r"```(?:json)?\s*\n?(.*?)```", content, re.DOTALL)
    json_str = fence.group(1).strip() if fence else content.strip()

    # If the model emitted reasoning before raw JSON without a fence, try to
    # locate the first { and last } and parse between them.
    if not fence and not json_str.startswith("{") and not json_str.startswith("["):
        first_brace = json_str.find("{")
        last_brace = json_str.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            json_str = json_str[first_brace : last_brace + 1]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        repaired = repair_json(json_str, return_objects=True)
        if repaired == "" and json_str.strip() not in ('""', "''"):
            raise ValueError(f"could not parse JSON from LLM response: {e}\n---\n{content}") from e
        return repaired


# ── cache management helpers (admin use) ────────────────────────────────────

def cache_stats() -> dict[str, Any]:
    return {
        "directory": str(_CACHE_DIR),
        "size_bytes": cache.volume(),
        "count": len(cache),
    }


def cache_clear() -> int:
    """Wipe the cache. Returns count of entries removed."""
    n = len(cache)
    cache.clear()
    log.warning("llm.cache_cleared", count=n)
    return n
