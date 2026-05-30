"""Auto-distiller — converts implicit-learning signal into Twin updates.

Triggered AUTOMATICALLY (not user-invoked) after Modify decisions accumulate.
Runs in the background. Surfaces a normal preview_action card with proposed
fs_write actions when it finds a Zack-specific pattern worth recording.

Per Zack 2026-05-26 v3:
  - 不应该由我主动来触发吧  → background, not user-initiated.
  - 整理完之后得告诉我      → card on HUD when proposals exist.
  - 我可以给一些反馈        → Approve/Modify/Kill, same as any agent output.

The trigger policy is intentionally conservative (cooldown + min-modify
threshold) so the user isn't bombarded. The distiller agent ITSELF
decides whether the pattern is stable enough to warrant a Twin write —
if no pattern, it emits actions:[] and cortex drops the card silently.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# Cooldown: don't run distillation more often than this regardless of trigger count.
DISTILL_COOLDOWN = timedelta(minutes=30)
# Min Modify decisions since last successful distill before we trigger.
DISTILL_MIN_MODIFIES = 2
# How many recent entries the distiller agent looks at.
DISTILL_LOOKBACK = 12


def build_distill_brief(
    *,
    now_iso: str,
    recent_entries: list[dict[str, Any]],
    available_dirs: list[str],
) -> str:
    """Brief for the distiller agent. Different shape from the user-invoke
    brief: focused on pattern recognition over learning queue corpus.

    Output contract: SAME as user-invoke (summary + actions[] + notes), so
    cortex's existing card builder + executor mapper just work.
    """
    L: list[str] = []
    L.append("== TASK ==")
    L.append("You're the Twin DISTILLER. Look at Zack's recent corrections (Modify decisions")
    L.append("with feedback text) and his Approve outcomes. Identify ONE or TWO stable")
    L.append("Zack-specific patterns worth recording in his Twin. Then propose minimal fs_write")
    L.append("actions to update the Twin.")
    L.append("")
    L.append(f"NOW: {now_iso}")
    L.append("")
    L.append("== RECENT INTERACTIONS (most recent last) ==")
    for i, e in enumerate(recent_entries, start=1):
        decision = e.get("decision", "?")
        ts = (e.get("ts") or "")[:19]
        ask = (e.get("user_ask") or "")[:200]
        L.append(f"[{i}] {ts} · {decision} · ask: {ask!r}")
        prop = e.get("agent_proposal") or {}
        title = prop.get("title") or ""
        if title:
            L.append(f"     proposed: {title[:160]}")
        if e.get("correction"):
            L.append(f"     correction: {e['correction'][:300]!r}")
    L.append("")

    L.append("== OUTPUT (same schema as user-invoke agent) ==")
    L.append("```")
    L.append("{")
    L.append('  "summary": "1-line — what pattern you found and what you propose",')
    L.append('  "actions": [ ... ],   // 0+ fs_write actions; EMPTY if no stable pattern emerged')
    L.append('  "notes":   "what you considered but decided NOT to act on, brief"')
    L.append("}")
    L.append("```")
    L.append("Action shape: { type:\"fs_write\", path: \"~/constellation/twin/...\", content: \"...\" }")
    L.append("")

    L.append("== RULES ==")
    L.append("R1  EMPTY actions[] is the right answer when:")
    L.append("    - Only 1-2 corrections (not enough signal)")
    L.append("    - The corrections are about distinct, unrelated topics (no pattern)")
    L.append("    - The corrections are one-shot facts already captured in their corresponding")
    L.append("      agent fs_write outputs (no further Twin update needed)")
    L.append("    A no-op is BETTER than a hallucinated 'pattern'. Don't pad.")
    L.append("")
    L.append("R2  When you DO propose a write, follow ~/constellation/twin/README.md exactly:")
    L.append("    - new skill → .claude/skills/<name>/SKILL.md (Anthropic format, `description:` field)")
    L.append("    - new/updated person → people/core/<slug>.md (aliases / relation / email only)")
    L.append("    - identity tweak → identity.md (rarely; only for foundational shifts)")
    L.append("")
    L.append("R3  Never write OUTSIDE ~/constellation/twin/. Never invent top-level directories.")
    L.append("")
    L.append("R4  For UPDATES to existing files (e.g. correcting a person profile), Read the")
    L.append("    current file first, then emit the FULL new content in fs_write.")
    L.append("    For NEW files (new person / new skill), follow the README skeleton.")
    L.append("")
    L.append("R5  Side effects beyond fs_write under twin/ are forbidden. No mail / reminder /")
    L.append("    calendar / imessage. This dispatch is for Twin maintenance only.")
    L.append("")

    L.append("== ZACK'S TWIN (read README first for the contract) ==")
    L.append("~/constellation/twin/")
    L.append("  README.md                 the contract — read FIRST before writing")
    L.append("  identity.md               core archive")
    L.append("  people/core/<slug>.md     person profiles")
    L.append("  .claude/skills/<n>/SKILL.md  Anthropic Agent Skills")
    L.append("  _system/learning_queue.jsonl   ← where these corrections come from")
    L.append("")
    if available_dirs:
        L.append("== --add-dir PATHS ==")
        for d in available_dirs:
            L.append(f"  {d}")
        L.append("")

    L.append("== APPROACH ==")
    L.append("1. Read ~/constellation/twin/README.md (the contract).")
    L.append("2. Skim the recent interactions above; look for patterns:")
    L.append("   - Repeated correction of the same shape (style, tone, format)")
    L.append("   - New person fact mentioned in correction text")
    L.append("   - Repeated rejection of a phrasing → potential skill")
    L.append("3. If you find ONE clear pattern, propose ONE or TWO fs_writes. If you don't,")
    L.append("   emit actions:[] and explain briefly in notes:. NEVER force a pattern.")
    L.append("4. Output JSON. End your turn.")
    L.append("")
    return "\n".join(L)


class Distiller:
    """Cortex-side trigger + dispatch for the auto-distiller.

    Lifecycle: registered as an attribute on CortexServer in __init__. Every
    time _handle_user_decision sees a Modify decision (with non-empty
    correction text), it calls `self.distiller.on_modify(...)`. Distiller
    decides whether to trigger; if yes, spawns a background task that
    dispatches a custom-brief agent run.
    """

    def __init__(self, server: Any) -> None:
        self.server = server
        # Track Modify-with-text count since last successful distill.
        self._unprocessed_modifies = 0
        self._last_distill_at: datetime | None = None
        self._in_flight = False  # don't overlap distill runs

    def on_modify(self, *, has_correction_text: bool) -> None:
        """Hook called from _handle_user_decision after a Modify."""
        if not has_correction_text:
            return
        self._unprocessed_modifies += 1
        log.info(
            "distiller.modify_observed",
            unprocessed=self._unprocessed_modifies,
            cooldown_until=(
                (self._last_distill_at + DISTILL_COOLDOWN).isoformat()
                if self._last_distill_at else None
            ),
        )
        if self._should_trigger():
            asyncio.create_task(self._run())

    def _should_trigger(self) -> bool:
        if self._in_flight:
            return False
        if self._unprocessed_modifies < DISTILL_MIN_MODIFIES:
            return False
        if self._last_distill_at is not None:
            if (datetime.now(timezone.utc) - self._last_distill_at) < DISTILL_COOLDOWN:
                return False
        return True

    def force_run(self) -> dict[str, Any]:
        """Bypass cooldown + threshold and dispatch immediately. Used by the
        /api/dev/distill_now endpoint for testing and for the (rare) case
        where the user wants to trigger distillation manually from the web
        console."""
        if self._in_flight:
            return {"ok": False, "error": "distill already in flight"}
        asyncio.create_task(self._run())
        return {"ok": True, "queued": True}

    async def _run(self) -> None:
        """Dispatch a distiller agent. Surfaces a card if it proposes anything."""
        self._in_flight = True
        try:
            from .schema import Event
            from . import ids
            from .agent_brief import CANONICAL_ACTIONS_SCHEMA

            twin_root = self.server.twin.root
            recent = self._read_recent_learning_entries(twin_root, n=DISTILL_LOOKBACK)
            if not recent:
                log.info("distiller.no_entries")
                return
            # Filter to modifies-with-text + recent approves for context.
            filtered = [e for e in recent if e.get("decision") in ("modify", "approve")][-DISTILL_LOOKBACK:]
            if not any(e.get("decision") == "modify" and e.get("correction") for e in filtered):
                log.info("distiller.no_modify_signal")
                return

            now_iso = datetime.now(timezone.utc).astimezone().isoformat()
            available_dirs = [
                os.path.expanduser("~/constellation/twin"),
            ]
            brief = build_distill_brief(
                now_iso=now_iso,
                recent_entries=filtered,
                available_dirs=available_dirs,
            )

            # Synthetic event so the rest of the pipeline (session log, card
            # builder, executor) just works. Marked with _origin so the
            # session log + UI can distinguish system-initiated runs.
            event = Event(
                id=ids.event_id(),
                kind="user_invoke",
                ts=datetime.now(timezone.utc),
                payload={
                    "text": "(auto) review recent corrections; propose Twin updates",
                    "_origin": "distiller",
                },
            )
            # Register a fresh session for this distill run so the timeline
            # is queryable in /sessions.
            sid = self.server.sessions.start_turn(
                existing_session_id=None,
                event_id=event.id, ask_text="(auto) distill", has_image=False,
            )
            event.payload["session_id"] = sid
            self.server._event_to_session[event.id] = sid
            # P0.3 — attribute LLM calls in this distill run to its session.
            from .sessions import current_session_id as _csid
            _csid.set(sid)
            self.server.sessions.append(
                sid, "distiller_start",
                unprocessed_modifies=self._unprocessed_modifies,
                lookback=len(filtered),
            )
            self.server.sessions.set_title(sid, "(auto) Twin distillation")

            # Dispatch claude_code.agent directly with our custom brief.
            await self.server._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="distiller_dispatch", icon="🔄",
                detail=f"reviewing {len(filtered)} recent interactions for patterns",
            )
            try:
                # Twin auto-distillation runs the complex agent in-process via the
                # SDK single source (Rev 18 C-72) — same rpc_result shape as the
                # retired tmux dispatch. Background work, no wearer to approve →
                # bypassPermissions (no permission cards).
                from .claude_sdk_agent import SdkAgentSession
                rpc_result = await SdkAgentSession(
                    self.server, event, brief=brief,
                    schema_hint=CANONICAL_ACTIONS_SCHEMA, add_dirs=available_dirs,
                    working_dir=os.path.expanduser("~"),
                    permission_mode="bypassPermissions", timeout_s=180.0,
                ).run()
            except Exception as e:
                log.warning("distiller.dispatch_failed", error=str(e))
                self.server.sessions.append(sid, "distiller_failed", error=str(e))
                return

            rpc_result = rpc_result or {}
            structured = rpc_result.get("structured") or {}
            actions = structured.get("actions") if isinstance(structured.get("actions"), list) else []

            if not actions:
                # No pattern found — emit a soft "I looked, nothing to do"
                # progress frame (per Zack 2026-05-25: "整理完之后告诉我"),
                # then drop. No card on HUD — would be spam.
                notes = (structured.get("notes") or "").strip()
                log.info(
                    "distiller.no_actions_proposed",
                    notes=notes or "(none)",
                )
                await self.server._emit_progress_to_glass(
                    parent_event_id=event.id,
                    stage="distiller_quiet", icon="🪞",
                    detail=(
                        f"Twin distiller looked at {len(filtered)} interactions — "
                        f"nothing to update right now."
                        + (f" ({notes[:80]})" if notes else "")
                    )[:200],
                )
                self.server.sessions.append(
                    sid, "distiller_no_actions",
                    notes=notes,
                )
                self._unprocessed_modifies = 0
                self._last_distill_at = datetime.now(timezone.utc)
                return

            # We have something. Surface a card via the normal helper.
            log.info("distiller.proposing", n_actions=len(actions))
            await self.server._emit_progress_to_glass(
                parent_event_id=event.id,
                stage="distiller_proposing", icon="🪞",
                detail=f"Twin distiller proposes {len(actions)} update(s) — review on HUD",
            )
            await self.server._send_agent_card_for_decision(
                rpc_result, event,
                working_dir=os.path.expanduser("~"),
                timeout_s=180.0,
            )
            self.server.sessions.append(
                sid, "distiller_proposed",
                n_actions=len(actions),
                summary=structured.get("summary") or "",
            )
            self._unprocessed_modifies = 0
            self._last_distill_at = datetime.now(timezone.utc)
        except Exception as e:
            log.error("distiller.unexpected", error=str(e), exc_info=True)
        finally:
            self._in_flight = False

    @staticmethod
    def _read_recent_learning_entries(twin_root: Path, n: int) -> list[dict[str, Any]]:
        """Tail the last n entries of learning_queue.jsonl. Cheap because the
        file is append-only and small (≤ N kB usually)."""
        p = Path(str(twin_root)) / "_system" / "learning_queue.jsonl"
        if not p.exists():
            return []
        try:
            with p.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        recent = []
        for raw in lines[-n:]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                recent.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return recent
