"""Phase 7a — Insight Engine (proactive surfacing).

A periodic background task that runs every CHECK_INTERVAL seconds and asks
each registered provider for new insights. Insights flow to Glass as
hud_show frames (info-only, no buttons — must not interrupt user agency).

Design (Zack 2026-05-25 reflection — surface only at quiet moments;
auto-trigger > manual review):
  - Default OFF (set INSIGHT_ENGINE_ENABLED=1 to turn on).
  - Each provider remembers what it has already surfaced (cooldown TTL).
  - A provider failure does NOT stop the loop; logged and continued.
  - hud_show only: no Approve/Modify/Kill. The user can act on the
    referenced thing themselves (open Reminders, reply to email, etc.).

Wiring: CortexServer.start_insight_engine() called from serve() after
the WSS listener is up. The engine is best-effort — if env is off, it's
a no-op.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)


# Tick interval. 15 minutes is conservative — feel free to lower once you
# trust the providers don't repeat-fire.
CHECK_INTERVAL_S = float(os.environ.get("CONSTELLATION_INSIGHT_TICK_S", "900"))
# Minimum gap between *any* insight surfacing, regardless of provider.
GLOBAL_COOLDOWN_S = float(os.environ.get("CONSTELLATION_INSIGHT_COOLDOWN_S", "600"))


class Insight:
    """One thing worth telling the user about, right now."""

    def __init__(
        self,
        *,
        kind: str,           # "reminder" | "calendar" | "email" | "weather" ...
        title: str,
        body: str,
        icon: str = "✦",
        # Stable identity — used to detect repeats within a cooldown window.
        dedup_key: str | None = None,
        # How long until we'd surface the same dedup_key again.
        cooldown: timedelta = timedelta(hours=2),
    ) -> None:
        self.kind = kind
        self.title = title
        self.body = body
        self.icon = icon
        self.dedup_key = dedup_key or f"{kind}::{title}"
        self.cooldown = cooldown


# Provider type: async fn that returns 0+ Insights. Receives the server
# so it can dispatch tool RPCs (read calendar, reminders, weather, etc.).
Provider = Callable[[Any], Awaitable[list[Insight]]]


class InsightEngine:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.enabled = os.environ.get("CONSTELLATION_INSIGHT_ENGINE", "0") == "1"
        self._task: asyncio.Task | None = None
        self._providers: list[Provider] = []
        # dedup_key → datetime last surfaced
        self._last_surfaced: dict[str, datetime] = {}
        self._global_last: datetime | None = None
        self._tick_count = 0

    def register(self, provider: Provider) -> None:
        self._providers.append(provider)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not self.enabled:
            log.info("insight_engine.disabled", reason="env CONSTELLATION_INSIGHT_ENGINE!=1")
            return
        if not self._providers:
            log.info("insight_engine.disabled", reason="no providers registered")
            return
        self._task = asyncio.create_task(self._loop())
        log.info(
            "insight_engine.started",
            tick_s=CHECK_INTERVAL_S, cooldown_s=GLOBAL_COOLDOWN_S,
            n_providers=len(self._providers),
        )

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        # First tick deferred a bit so we don't fire the moment the server
        # boots (useful if launchd auto-restarts mid-conversation).
        await asyncio.sleep(min(60.0, CHECK_INTERVAL_S))
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("insight_engine.tick_failed", error=str(e), exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def _tick(self) -> None:
        self._tick_count += 1
        log.info("insight_engine.tick", n=self._tick_count, n_providers=len(self._providers))
        now = datetime.now(timezone.utc)
        if self._global_last and (now - self._global_last).total_seconds() < GLOBAL_COOLDOWN_S:
            log.info("insight_engine.global_cooldown", until=self._global_last + timedelta(seconds=GLOBAL_COOLDOWN_S))
            return
        # If no Glass connection, don't bother — nothing to surface to.
        if not getattr(self.server, "_glass_conn", None):
            log.info("insight_engine.no_glass_skip")
            return

        for provider in self._providers:
            try:
                insights = await provider(self.server)
            except Exception as e:
                log.warning("insight_engine.provider_failed", provider=getattr(provider, "__name__", "?"), error=str(e))
                continue
            for ins in insights:
                last = self._last_surfaced.get(ins.dedup_key)
                if last and (now - last) < ins.cooldown:
                    continue
                await self._surface(ins)
                self._last_surfaced[ins.dedup_key] = now
                self._global_last = now
                # One insight per tick max — avoid burst.
                return

    async def _surface(self, ins: Insight) -> None:
        """Send a hud_show command for this insight."""
        from .schema import Command
        from . import ids
        cmd = Command(
            id=ids.command_id(), ts=datetime.now(timezone.utc),
            kind="hud_show",
            payload={
                "title": ins.title[:80],
                "body": ins.body[:1500],
                "icon": ins.icon,
                "options": [],
                "_insight_kind": ins.kind,
            },
            requires_confirm=False, ttl_ms=30_000,
        )
        try:
            await self.server._glass_conn.send(cmd.model_dump_json())
            log.info("insight.surfaced", kind=ins.kind, dedup_key=ins.dedup_key, title=ins.title[:60])
        except Exception as e:
            log.warning("insight.send_failed", error=str(e))


# ── Default providers ────────────────────────────────────────────────────


async def upcoming_reminders_provider(server: Any) -> list[Insight]:
    """Surface reminders due in the next 30 minutes (so user notices before
    the system notification fires). No-op if applescript_reminders adapter
    is missing or returns nothing."""
    # Use the fast-path AppleScript-side filter (added 2026-05-26). On a
    # ~100-reminder list this drops adapter latency from ~30s → ~1s because
    # Reminders.app evaluates `whose ... due date ≤ now+30m` natively.
    try:
        rpc = await server._dispatch_to_tool({
            "tool": "applescript_reminders", "action": "list",
            "args": {"completed": False, "due_within_minutes": 30, "limit": 10},
            "result_format": "query",
        })
    except Exception as e:
        log.warning("insight.reminders.dispatch_failed", error=str(e))
        return []
    result = rpc.result or {}
    items = result.get("items") or []
    log.info(
        "insight.reminders.fetched",
        n_items=len(items),
        with_due=sum(1 for r in items if isinstance(r, dict) and r.get("due")),
    )
    now = datetime.now(timezone.utc)
    window = timedelta(minutes=30)
    out: list[Insight] = []
    for r in items:
        if not isinstance(r, dict):
            continue
        due = r.get("due") or r.get("due_date")
        if not due:
            continue
        try:
            dt = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.astimezone()
        except Exception:
            continue
        delta = dt.astimezone(timezone.utc) - now
        if timedelta(0) <= delta <= window:
            title = (r.get("title") or r.get("name") or "Reminder").strip()
            mins = max(1, int(delta.total_seconds() // 60))
            out.append(Insight(
                kind="reminder",
                title=f"Heads up — {title}",
                body=f"Reminder due in {mins} min: {title}",
                icon="⏰",
                dedup_key=f"reminder::{title}::{dt.isoformat()}",
                cooldown=timedelta(hours=4),
            ))
    return out


def register_default_providers(engine: InsightEngine) -> None:
    """Wire up the starter set. Add new providers here as they ship."""
    engine.register(upcoming_reminders_provider)
