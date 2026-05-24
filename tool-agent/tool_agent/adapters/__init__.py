"""Tool adapters. Each adapter implements the ToolAdapter protocol below.

See ../../TOOL-ADAPTERS.md for the per-adapter action catalogs.
"""

from __future__ import annotations

from typing import Any, Protocol


class ToolAdapter(Protocol):
    """All tool adapters implement this. See TOOL-ADAPTERS.md §contract."""

    name: str

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        """Execute the action. Returns the result dict that goes into RPCResult.result.

        Raises on unknown action or bad args; caller wraps the exception into
        RPCResult { status: failure }.
        """
        ...
