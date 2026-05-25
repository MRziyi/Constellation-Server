"""Tool Agent WebSocket server — exposes registered adapters to Cortex.

v2: supports adapter-initiated event push back to Cortex (for reverse-wake patterns).
Adapters that need this capability accept `event_pusher: Callable[[dict], Awaitable[None]]`
either via constructor or post-instantiation wiring.

Message shapes flowing over the connection:
  Cortex → Tool Agent : RPCDispatch     {id, tool, action, args, ...}
  Tool Agent → Cortex : RPCResult       {id, status, result, ...}   (response to dispatch)
  Tool Agent → Cortex : Event           {kind, payload, ts}         (push, no id)

Cortex's reader demuxes by presence of `id` (RPCResult) vs `kind` (Event).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import structlog
import websockets
from websockets.asyncio.server import ServerConnection

from .registry import ToolRegistry

log = structlog.get_logger(__name__)


class ToolAgentServer:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        # Active Cortex connection (Tool Agent only handles one Cortex at a time in v1).
        self._cortex_conn: ServerConnection | None = None
        # Wire each adapter that accepts event-push capability.
        for name in registry.names:
            adapter = registry.get(name)
            if hasattr(adapter, "attach_event_pusher"):
                adapter.attach_event_pusher(self._push_event)

    async def _push_event(self, event: dict[str, Any]) -> None:
        """Send an unsolicited event to the connected Cortex (if any)."""
        if self._cortex_conn is None:
            log.warning("event.dropped_no_cortex_conn", kind=event.get("kind"))
            return
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        try:
            await self._cortex_conn.send(json.dumps(event))
            log.info("event.pushed", kind=event.get("kind"))
        except Exception as e:
            log.error("event.push_failed", error=str(e), kind=event.get("kind"))

    async def handle(self, ws: ServerConnection) -> None:
        log.info("cortex.connected", remote=ws.remote_address)
        self._cortex_conn = ws
        # Each RPC runs in its OWN task so a long-running action (e.g.
        # claude_code.agent that tails session.jsonl for 30-60s) doesn't
        # block subsequent dispatches like send_keys (which mid-flight
        # corrections rely on). Sequential awaits used to deadlock the
        # cortex.feedback path.
        inflight: set[asyncio.Task] = set()

        async def _run_and_reply(msg: dict) -> None:
            try:
                result = await self._dispatch(msg)
                await ws.send(json.dumps(result))
            except Exception as e:
                # _dispatch already turns adapter errors into {status:failure};
                # this catch is only for transport-level issues (e.g. ws closed
                # mid-reply). Don't crash the connection handler.
                log.error("rpc.reply_failed", error=str(e), rpc_id=msg.get("id"))

        try:
            async for raw in ws:
                msg = json.loads(raw)
                if "tool" in msg and "action" in msg:
                    task = asyncio.create_task(_run_and_reply(msg))
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
                else:
                    log.warning("unknown_message_shape", keys=list(msg.keys()))
        except websockets.exceptions.ConnectionClosed:
            log.info("cortex.disconnected")
        finally:
            # Let in-flight tasks finish (or get cancelled by ws close).
            # Cancelling here would interrupt long-running agents on
            # reconnect; let them complete or fail naturally.
            for t in inflight:
                if not t.done():
                    t.cancel()
            if self._cortex_conn is ws:
                self._cortex_conn = None

    async def _dispatch(self, msg: dict) -> dict:
        rpc_id = msg.get("id", "rpc_unknown")
        try:
            adapter = self.registry.get(msg["tool"])
            result = await adapter.dispatch(
                action=msg["action"],
                args=msg.get("args", {}),
                context_pack=msg.get("context_pack", []),
                result_format=msg.get("result_format", "execute"),
            )
            return {
                "id": rpc_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": "success",
                "result": result,
                "diagnostics": None,
            }
        except Exception as e:
            log.error("dispatch_failed", error=str(e), tool=msg.get("tool"))
            return {
                "id": rpc_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": "failure",
                "result": {},
                "diagnostics": str(e),
            }


async def serve(host: str, port: int, registry: ToolRegistry) -> None:
    server = ToolAgentServer(registry)
    log.info("tool-agent.listening", host=host, port=port, tools=registry.names)
    async with websockets.serve(server.handle, host, port):
        await asyncio.Future()
