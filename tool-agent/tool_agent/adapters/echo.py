"""Echo adapter — Phase 1 only. Validates the spine works end-to-end."""

from __future__ import annotations

from typing import Any


class EchoAdapter:
    name = "echo"

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        if action != "echo":
            raise ValueError(f"echo adapter only supports action='echo', got '{action}'")
        return {"echoed": args.get("text", "<no text>")}
