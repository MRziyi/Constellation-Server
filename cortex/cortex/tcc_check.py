"""TCC self-check — verify macOS app permissions Cortex needs at startup.

macOS gates AppleScript access to apps (Mail, Calendar, Reminders, …) behind
TCC (Transparency, Consent, Control). If permission isn't granted, every
adapter call fails with osascript exit code -1743 ("Not authorized to send
Apple events to <App>"). Without an explicit check, the user gets a
confusing string of card failures days later — better to fail loud at boot.

Run at Cortex startup (post-listener). Each probe is bounded ~1s.
Failures don't kill the process; they surface a hud_show once Glass connects.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# Each entry: (app_name, no-op AppleScript snippet, friendly_label).
# Keep snippets cheap (`count messages`, `count reminders`, …).
PROBES: list[tuple[str, str, str]] = [
    ("Mail", 'tell application "Mail" to count messages of inbox of first account', "Mail.app"),
    ("Calendar", 'tell application "Calendar" to get name of every calendar', "Calendar.app"),
    ("Reminders", 'tell application "Reminders" to count reminders of list "Reminders"', "Reminders.app"),
    ("Messages", 'tell application "Messages" to count of chats', "Messages.app (iMessage)"),
    ("Notes", 'tell application "Notes" to count notes', "Notes.app"),
]


async def _probe_one(app: str, script: str, label: str) -> tuple[str, bool, str]:
    """Run one probe. Returns (app, ok, detail)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return (app, False, f"{label} probe timed out (>8s)")
        if proc.returncode == 0:
            return (app, True, stdout.decode("utf-8", errors="replace").strip()[:80])
        err = stderr.decode("utf-8", errors="replace").strip()
        # TCC denial signature: "Not authorized to send Apple events to <App>"
        # rc usually 1; AppleScript errors hide the deeper code in stderr.
        is_tcc = "Not authorized" in err or "1743" in err or "-1743" in err
        reason = "TCC denied" if is_tcc else f"rc={proc.returncode}"
        return (app, False, f"{label}: {reason} — {err[:120]}")
    except Exception as e:
        return (app, False, f"{label}: probe error {type(e).__name__}: {e}")


async def run_tcc_check() -> list[tuple[str, bool, str]]:
    """Run all probes in parallel. Returns the result list."""
    log.info("tcc_check.start", n_probes=len(PROBES))
    results = await asyncio.gather(*[_probe_one(a, s, l) for a, s, l in PROBES])
    n_ok = sum(1 for _, ok, _ in results if ok)
    n_fail = len(results) - n_ok
    log.info(
        "tcc_check.done", n_ok=n_ok, n_fail=n_fail,
        failed=[a for a, ok, _ in results if not ok],
    )
    return list(results)


async def run_and_surface(server: Any) -> None:
    """Run probes; if any fail, queue a hud_show for the next Glass connect.

    Called at server startup. Survives the absence of Glass — the warning
    lands as soon as Glass connects (handled by the server's
    `_pending_startup_card` slot).
    """
    results = await run_tcc_check()
    failed = [(a, det) for a, ok, det in results if not ok]
    if not failed:
        return
    body_lines = ["The following macOS apps need permission for Constellation to work:", ""]
    for app, detail in failed:
        body_lines.append(f"• **{app}** — {detail[:160]}")
    body_lines.append("")
    body_lines.append("Grant in: System Settings → Privacy & Security → Automation → claude-code (or your terminal).")
    body = "\n".join(body_lines)
    log.warning(
        "tcc_check.permissions_missing",
        n_failed=len(failed),
        apps=[a for a, _ in failed],
    )
    # Stash on the server; it gets sent on the first Glass connection (cortex
    # boots before Glass usually). If Glass is already connected, send now.
    setattr(server, "_pending_startup_card", {
        "title": f"TCC permissions missing ({len(failed)})",
        "body": body[:1800],
        "icon": "⚠",
    })
    if getattr(server, "_glass_conn", None):
        try:
            from .schema import Command
            from . import ids
            from datetime import datetime, timezone
            cmd = Command(
                id=ids.command_id(), ts=datetime.now(timezone.utc),
                kind="hud_show",
                payload={
                    "title": f"TCC permissions missing ({len(failed)})",
                    "body": body[:1800],
                    "icon": "⚠",
                    "options": [],
                },
                requires_confirm=False, ttl_ms=60_000,
            )
            await server._glass_conn.send(cmd.model_dump_json())
        except Exception as e:
            log.warning("tcc_check.send_failed", error=str(e))
