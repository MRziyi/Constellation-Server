"""claude_code adapter — invoke Anthropic's Claude Code CLI on the Mac.

Track A — non-interactive `claude -p` mode (no tmux). Actions: draft, run,
  continue_, list_sessions. Covers email drafts, web/paper search (per SoT N-9),
  "let CC look at directory X and tell me", code generation, summaries. Each call
  is a one-shot subprocess; `--session-id + --resume` lets the user continue the
  conversation even though each call exits.

(The former Track B — interactive `claude` driven inside tmux, the reverse-wake
watcher, and the jsonl-tailing streaming-agent — was retired with the tmux
removal. The complex-agent path now runs in-process via the Claude Agent SDK
in cortex/cortex/claude_sdk_agent.py.)

Per SoT N-9: routes web/paper/arxiv/Tavily-style search through Track A (CC has
WebFetch). Per Zack 2026-05-24: the biggest single use case is "让 Claude Code 去
不同目录里看东西" — Track A's `--add-dir` arg is the natural fit.

Cost guardrail: `--max-budget-usd` defaults to 0.50 USD per call.
Permission default: `--permission-mode dontAsk`.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any


DEFAULT_PERMISSION_MODE = "dontAsk"
DEFAULT_MAX_BUDGET_USD = 0.50
DEFAULT_TIMEOUT_S = 300.0  # 5 min — single CC -p call shouldn't need more
DEFAULT_OUTPUT_FORMAT = "json"  # we parse this for the structured result


def _new_session_id() -> str:
    return str(uuid.uuid4())


async def _run_claude(
    prompt: str,
    *,
    working_dir: str | None = None,
    add_dirs: list[str] | None = None,
    model: str | None = None,
    session_id: str | None = None,
    resume: bool = False,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
) -> dict[str, Any]:
    """Spawn `claude -p` subprocess; return parsed result dict."""
    cmd = ["claude", "-p", "--permission-mode", permission_mode, "--max-budget-usd", str(max_budget_usd),
           "--output-format", output_format]
    if model:
        cmd.extend(["--model", model])
    if session_id:
        cmd.extend(["--session-id", session_id])
    if resume:
        cmd.extend(["--resume", session_id]) if session_id else cmd.append("--continue")
    if add_dirs:
        cmd.append("--add-dir")
        cmd.extend(add_dirs)
    if allowed_tools:
        cmd.append("--allowedTools")
        cmd.extend(allowed_tools)
    if disallowed_tools:
        cmd.append("--disallowedTools")
        cmd.extend(disallowed_tools)
    cmd.append(prompt)

    cwd = working_dir or os.getcwd()
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return {
                "ok": False,
                "error": f"claude -p timed out after {timeout_s}s",
                "session_id": session_id,
                "started_at": started_at,
                "command": cmd,
            }
        rc = proc.returncode or 0
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return {"ok": False, "error": "claude CLI not found in PATH"}

    finished_at = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "ok": rc == 0,
        "rc": rc,
        "session_id": session_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "cwd": cwd,
        "stderr": stderr.strip() or None,
    }
    if output_format == "json" and stdout.strip():
        try:
            parsed = json.loads(stdout)
            # CC -p --output-format=json returns {result, num_turns, cost_usd, ...}
            result["text"] = parsed.get("result", "")
            result["cost_usd"] = parsed.get("cost_usd") or parsed.get("total_cost_usd")
            result["num_turns"] = parsed.get("num_turns")
            result["raw_json"] = parsed
        except json.JSONDecodeError:
            result["text"] = stdout
            result["parse_error"] = "json output_format requested but stdout wasn't valid JSON"
    else:
        result["text"] = stdout
    return result


class ClaudeCodeAdapter:
    """In-memory Track-A session tracking (not persistent across daemon restarts)."""

    name = "claude_code"
    SIDE_EFFECT_ACTIONS = {"draft", "run", "continue_"}

    def __init__(self) -> None:
        # Track A (-p) sessions: claude session_id → metadata
        self._sessions: dict[str, dict[str, Any]] = {}

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        if action in self.SIDE_EFFECT_ACTIONS and result_format == "draft":
            if action != "draft":
                raise ValueError(
                    f"claude_code.{action} has no Cortex-draft semantics. "
                    f"Use result_format='execute' with requires_confirm=true."
                )

        if action == "draft":
            return await self._draft(args)
        if action == "run":
            return await self._run(args)
        if action in ("continue", "continue_"):
            return await self._continue(args)
        if action == "list_sessions":
            return await self._list_sessions(args)

        raise ValueError(f"claude_code: unknown action '{action}'")

    # ── actions ──

    async def _draft(self, args: dict[str, Any]) -> dict[str, Any]:
        """One-shot text generation. Does NOT track a session.

        Use for: 'have CC look at X and tell me what it says', web/paper search, code
        snippets, summaries. CC may use its own tools (Read/Grep/WebFetch/...) but
        result is just the final text.
        """
        prompt = args.get("prompt")
        if not prompt:
            raise ValueError("claude_code.draft: 'prompt' required")
        result = await _run_claude(
            prompt=prompt,
            working_dir=args.get("working_dir"),
            add_dirs=args.get("add_dirs"),
            model=args.get("model"),
            permission_mode=args.get("permission_mode", DEFAULT_PERMISSION_MODE),
            max_budget_usd=float(args.get("max_budget_usd", DEFAULT_MAX_BUDGET_USD)),
            allowed_tools=args.get("allowed_tools"),
            disallowed_tools=args.get("disallowed_tools"),
            timeout_s=float(args.get("timeout_s", DEFAULT_TIMEOUT_S)),
            output_format=args.get("output_format", DEFAULT_OUTPUT_FORMAT),
        )
        # Strip giant raw_json from response (still in adapter logs / debugging)
        result.pop("raw_json", None)
        return result

    async def _run(self, args: dict[str, Any]) -> dict[str, Any]:
        """Like draft, but assigns + tracks a session_id so user can resume later."""
        prompt = args.get("prompt")
        if not prompt:
            raise ValueError("claude_code.run: 'prompt' required")
        session_id = args.get("session_id") or _new_session_id()
        working_dir = args.get("working_dir") or os.getcwd()

        result = await _run_claude(
            prompt=prompt,
            working_dir=working_dir,
            add_dirs=args.get("add_dirs"),
            model=args.get("model"),
            session_id=session_id,
            permission_mode=args.get("permission_mode", DEFAULT_PERMISSION_MODE),
            max_budget_usd=float(args.get("max_budget_usd", DEFAULT_MAX_BUDGET_USD)),
            allowed_tools=args.get("allowed_tools"),
            disallowed_tools=args.get("disallowed_tools"),
            timeout_s=float(args.get("timeout_s", DEFAULT_TIMEOUT_S)),
            output_format=args.get("output_format", DEFAULT_OUTPUT_FORMAT),
        )
        result.pop("raw_json", None)

        self._sessions[session_id] = {
            "session_id": session_id,
            "working_dir": working_dir,
            "started_at": result.get("started_at"),
            "last_activity": result.get("finished_at"),
            "last_prompt": prompt[:200],
            "turn_count": (result.get("num_turns") or 1),
            "total_cost_usd": result.get("cost_usd") or 0.0,
        }
        return result

    async def _continue(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = args.get("session_id")
        prompt = args.get("prompt")
        if not session_id or not prompt:
            raise ValueError("claude_code.continue_: 'session_id' and 'prompt' required")
        meta = self._sessions.get(session_id)
        if not meta:
            # We may still resume; user might know an id from outside adapter memory
            meta = {"session_id": session_id, "working_dir": args.get("working_dir") or os.getcwd()}

        working_dir = args.get("working_dir") or meta.get("working_dir") or os.getcwd()
        result = await _run_claude(
            prompt=prompt,
            working_dir=working_dir,
            add_dirs=args.get("add_dirs"),
            model=args.get("model"),
            session_id=session_id,
            resume=True,
            permission_mode=args.get("permission_mode", DEFAULT_PERMISSION_MODE),
            max_budget_usd=float(args.get("max_budget_usd", DEFAULT_MAX_BUDGET_USD)),
            allowed_tools=args.get("allowed_tools"),
            disallowed_tools=args.get("disallowed_tools"),
            timeout_s=float(args.get("timeout_s", DEFAULT_TIMEOUT_S)),
            output_format=args.get("output_format", DEFAULT_OUTPUT_FORMAT),
        )
        result.pop("raw_json", None)

        meta["last_activity"] = result.get("finished_at")
        meta["last_prompt"] = prompt[:200]
        meta["turn_count"] = (meta.get("turn_count") or 0) + (result.get("num_turns") or 1)
        meta["total_cost_usd"] = (meta.get("total_cost_usd") or 0.0) + (result.get("cost_usd") or 0.0)
        self._sessions[session_id] = meta
        return result

    async def _list_sessions(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "count": len(self._sessions),
            "sessions": list(self._sessions.values()),
        }
