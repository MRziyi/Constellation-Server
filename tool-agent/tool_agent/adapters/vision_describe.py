"""vision_describe adapter — Q.4.5 / C-47.

A bounded single-call tool that describes an image attached to the user's
ask. Mirrors the twin_query pattern (OpenAI direct call) but with a
multimodal message containing the image bytes.

Routing contract (cortex side):
- Cortex `_VISION_AWARE_TOOLS = {"vision_describe"}`. Only when the router
  routes to this tool does `_image_b64` get injected into args. Other tools
  never see the image. (C-47 default-off guarantee.)
- The dispatcher itself never inspects image bytes — they arrive here as an
  opaque base64 string and we pass them straight to the multimodal API.

Cost characteristics:
- Single API call per invocation (no agent loop, no follow-up turns)
- Image at ≤1024px / q=80 ≈ 70 KB → base64 ~93 KB string → cheap on
  modern vision-capable models
- Fallback string when no image attached (defensive — router shouldn't
  pick us in that case, but be safe)

Env knobs:
- `OPENAI_API_KEY` — required (mirrors twin_query)
- `CORTEX_VISION_MODEL` — override the default vision-capable model
  (default: `gpt-5.2` — the project's standard model in 2026; multimodal)
"""

from __future__ import annotations

import os
from typing import Any

import structlog

log = structlog.get_logger(__name__)

DEFAULT_MODEL = os.environ.get("CORTEX_VISION_MODEL", "gpt-5.2")
DEFAULT_MAX_TOKENS = int(os.environ.get("CORTEX_VISION_MAX_TOKENS", "400"))
DEFAULT_SYSTEM = (
    "You are describing a frame captured by Zack's AR glasses. "
    "Be concise (≤80 words), specific, human. No preamble."
)


async def _describe(prompt: str, image_b64: str, *, model: str) -> dict[str, Any]:
    """Single multimodal call. Returns dict with description + tokens + model."""
    if not os.environ.get("OPENAI_API_KEY"):
        return {
            "description": "(vision_describe: OPENAI_API_KEY not set)",
            "model": "fallback",
            "tokens": {"input": 0, "output": 0},
        }
    if not image_b64:
        # Defensive: router shouldn't pick vision_describe without an image
        # attached, but if it does, return a clean acknowledgment rather
        # than spending an API call on whitespace.
        return {
            "description": "(no image attached)",
            "model": "fallback",
            "tokens": {"input": 0, "output": 0},
        }

    # OpenAI chat.completions multimodal format:
    #   content = [{type:"text", ...}, {type:"image_url", image_url:{url:"data:..."}}]
    image_url = f"data:image/jpeg;base64,{image_b64}"
    messages = [
        {"role": "system", "content": DEFAULT_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt or "Describe this image."},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI()
        # gpt-5.x uses `max_completion_tokens`; older gpt-4.x uses `max_tokens`.
        # Probe by model name to avoid runtime API errors on either family.
        token_kwarg = (
            {"max_completion_tokens": DEFAULT_MAX_TOKENS}
            if model.startswith("gpt-5") or model.startswith("o")
            else {"max_tokens": DEFAULT_MAX_TOKENS}
        )
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            **token_kwarg,
        )
        text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        return {
            "description": text,
            "model": resp.model,
            "tokens": {
                "input": getattr(usage, "prompt_tokens", 0),
                "output": getattr(usage, "completion_tokens", 0),
            },
        }
    except Exception as e:
        log.warning("vision_describe.call_failed", error=str(e), model=model)
        return {
            "description": f"(vision_describe: LLM call failed: {e})",
            "model": "fallback",
            "tokens": {"input": 0, "output": 0},
        }


class VisionDescribeAdapter:
    name = "vision_describe"

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        if action != "describe":
            raise ValueError(
                f"vision_describe: unknown action '{action}' (only 'describe')"
            )
        prompt = (args.get("prompt") or args.get("text") or "").strip()
        # `_image_b64` is the C-47 opaque passthrough — Cortex injects only
        # when router routes us. Leading underscore = "this is metadata,
        # don't put it in the prompt verbatim".
        image_b64 = args.get("_image_b64") or ""
        model = args.get("model") or DEFAULT_MODEL

        result = await _describe(prompt, image_b64, model=model)
        # Surface the description as both a top-level field AND as `summary`
        # so the existing agent-finished-card body renderer picks it up
        # cleanly without special-case logic.
        return {
            "description": result["description"],
            "summary": result["description"],
            "model": result["model"],
            "tokens": result["tokens"],
            "had_image": bool(image_b64),
        }
