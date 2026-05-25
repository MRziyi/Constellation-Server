"""claude_code adapter — invoke Anthropic's Claude Code CLI on the Mac.

Two-track design:

  TRACK A — non-interactive `claude -p` mode (no tmux). Actions: draft, run, continue_,
    list_sessions. Covers 80% of cases: email drafts, web/paper search (per SoT N-9),
    "let CC look at directory X and tell me", code generation, summaries. Each call is a
    one-shot subprocess; `--session-id + --resume` lets the user continue the conversation
    even though each call exits.

  TRACK B — interactive `claude` in tmux session. Actions: run_interactive, get_pane,
    send_keys, kill, list_tmux. Covers UC2 (双向遥控): user starts a long CC task in tmux,
    walks away with Glass; CC eventually hits a permission prompt; reverse-wake watcher
    (Chunk 2) detects it, pushes to Glass, user grants via voice → send_keys 'y\\n' →
    CC continues. Also covers any long-running supervision pattern.

Track selection (Router prompt guidance): use Track A for "one-shot answer" or "quick draft";
use Track B when the user wants CC to "go run X and tell me how it goes" or "be available
to take input for a while".

Per SoT N-9: routes web/paper/arxiv/Tavily-style search through Track A (CC has WebFetch).
Per Zack 2026-05-24: the biggest single use case is "让 Claude Code 去不同目录里看东西" —
Track A's `--add-dir` arg is the natural fit; Track B for sustained-iteration variants.

Cost guardrail (Track A only): `--max-budget-usd` defaults to 0.50 USD per call.
Permission default: `--permission-mode dontAsk` on Track A; Track B uses default
interactive permissions which is exactly what enables UC2's reverse-wake.

tmux details (Track B):
- Isolated socket `/tmp/cortex-tool-agent-cc.sock` so adapter sessions don't collide with
  user's own tmux sessions.
- Session name = `cc-<short_uuid>`.
- Patterns + response keys live in twin-seed/skills/claude-code-control.md (hot-reloadable).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


DEFAULT_PERMISSION_MODE = "dontAsk"
DEFAULT_MAX_BUDGET_USD = 0.50
DEFAULT_TIMEOUT_S = 300.0  # 5 min — single CC -p call shouldn't need more
DEFAULT_OUTPUT_FORMAT = "json"  # we parse this for the structured result

# Track B (tmux) constants
TMUX_SOCKET = "/tmp/cortex-tool-agent-cc.sock"
TMUX_SESSION_PREFIX = "cc-"
TMUX_DEFAULT_CAPTURE_LINES = 200

# Reverse-wake watcher constants
WATCHER_POLL_INTERVAL_S = 1.5
# CC v2.1.x permission UI (verified 2026-05-24): shows
#   "Do you want to proceed?"
#   " ❯ 1. Yes"
#   "   2. Yes, and always allow access to <scope>"
#   "   3. No"
# Arrow keys navigate; Enter selects. Default cursor is option 1.
PERMISSION_PATTERNS = [
    re.compile(r"Do you want to proceed\?", re.IGNORECASE),  # CC v2 modern
    re.compile(r"❯\s*1\.\s+Yes", re.MULTILINE),              # CC v2 menu cursor
    # Legacy patterns kept for older CC versions:
    re.compile(r"Do you want to .*\?\s*\(y/n\)", re.IGNORECASE),
    re.compile(r"Approve this action\?\s*\[y/N\]", re.IGNORECASE),
    re.compile(r"Allow .*\?\s*\[y/N\]", re.IGNORECASE),
    re.compile(r"Do you want to allow .*\?", re.IGNORECASE),
]
COMPLETION_PATTERNS = [
    re.compile(r"✓ Task complete", re.IGNORECASE),
    re.compile(r"✔ Done\.", re.IGNORECASE),
]
ERROR_PATTERNS = [
    re.compile(r"^Error:", re.MULTILINE),
    re.compile(r"^Traceback \(most recent call last\):", re.MULTILINE),
    re.compile(r"^fatal: ", re.MULTILINE),
]


def _new_session_id() -> str:
    return str(uuid.uuid4())


def _short_id() -> str:
    return uuid.uuid4().hex[:10]


async def _run(cmd: list[str], *, timeout: float = 30.0) -> tuple[int, str, str]:
    """Generic subprocess helper for tmux + shell commands."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return -1, "", str(e)


def _tmux(*args: str) -> list[str]:
    """Build a tmux command with our isolated socket."""
    return ["tmux", "-S", TMUX_SOCKET, *args]


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
    """In-memory session tracking for both tracks (not persistent across daemon restarts)."""

    name = "claude_code"
    SIDE_EFFECT_ACTIONS = {"draft", "run", "continue_", "run_interactive", "send_keys", "kill"}

    def __init__(self) -> None:
        # Track A (-p) sessions: claude session_id → metadata
        self._sessions: dict[str, dict[str, Any]] = {}
        # Track B (tmux) sessions: tmux session name → metadata
        self._tmux_sessions: dict[str, dict[str, Any]] = {}
        # session_id → asyncio.Task running the reverse-wake watcher
        self._watchers: dict[str, asyncio.Task] = {}
        # Server's event-push callable, wired post-construction
        self._event_pusher: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def attach_event_pusher(self, pusher: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Called by ToolAgentServer at startup."""
        self._event_pusher = pusher

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

        # Track A actions
        if action == "draft":
            return await self._draft(args)
        if action == "run":
            return await self._run(args)
        if action in ("continue", "continue_"):
            return await self._continue(args)
        if action == "list_sessions":
            return await self._list_sessions(args)

        # Track B (tmux) actions
        if action == "run_interactive":
            return await self._run_interactive(args)
        if action == "get_pane":
            return await self._get_pane(args)
        if action == "send_keys":
            return await self._send_keys(args)
        if action == "kill":
            return await self._kill(args)
        if action == "list_tmux":
            return await self._list_tmux(args)
        if action == "start_watcher":
            return await self._start_watcher_action(args)
        if action == "stop_watcher":
            return await self._stop_watcher_action(args)
        if action == "__test_inject_wake__":
            return await self._test_inject_wake(args)

        # v2 streaming agent (per AGENT-ARCHITECTURE-V2.md Phase 5a)
        if action == "agent":
            return await self._agent(args)
        if action == "agent_continue":
            return await self._agent_continue(args)
        if action == "agent_kill":
            return await self._agent_kill(args)

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

    # ── Track B (tmux-based interactive supervision) ──

    async def _run_interactive(self, args: dict[str, Any]) -> dict[str, Any]:
        """Start an interactive Claude Code session inside a detached tmux session.

        After this returns, CC is sitting at its prompt waiting for input. Cortex (or the
        user via reverse-wake interactions) can `send_keys` to drive it, `get_pane` to
        observe, `kill` to terminate. This is the path UC2 needs.

        Returns: {session_id, started_at, cwd, claude_args, state}
        """
        prompt = args.get("prompt")
        working_dir = args.get("working_dir") or os.getcwd()
        session_name = args.get("session_name") or f"{TMUX_SESSION_PREFIX}{_short_id()}"
        model = args.get("model")
        add_dirs = args.get("add_dirs") or []
        permission_mode = args.get("permission_mode")

        claude_args = ["claude"]
        if model:
            claude_args.extend(["--model", model])
        for d in add_dirs:
            claude_args.extend(["--add-dir", d])
        if permission_mode:
            claude_args.extend(["--permission-mode", permission_mode])

        cmd = _tmux("new-session", "-d", "-s", session_name, "-c", working_dir, *claude_args)
        rc, _, stderr = await _run(cmd, timeout=10.0)
        if rc != 0:
            raise RuntimeError(f"tmux new-session failed (rc={rc}): {stderr.strip()}")

        started_at = datetime.now(timezone.utc).isoformat()
        meta = {
            "session_id": session_name,
            "started_at": started_at,
            "last_activity": started_at,
            "cwd": working_dir,
            "claude_args": claude_args,
            "state": "starting",
            "initial_prompt": (prompt or "")[:200],
        }
        self._tmux_sessions[session_name] = meta

        if prompt:
            # Let CC's TUI render before sending input
            await asyncio.sleep(2.0)
            await _run(_tmux("send-keys", "-l", "-t", session_name, prompt), timeout=5.0)
            await _run(_tmux("send-keys", "-t", session_name, "Enter"), timeout=5.0)
            meta["state"] = "running"
            meta["last_activity"] = datetime.now(timezone.utc).isoformat()

        # Auto-start the reverse-wake watcher unless the caller explicitly opts out.
        if args.get("watch", True) and self._event_pusher is not None:
            self._start_watcher(session_name)
            meta["watcher_running"] = True
        else:
            meta["watcher_running"] = False

        return meta

    async def _get_pane(self, args: dict[str, Any]) -> dict[str, Any]:
        """Capture the live TUI content of a tmux session."""
        session_id = args.get("session_id") or args.get("session_name")
        if not session_id:
            raise ValueError("claude_code.get_pane: 'session_id' required")
        lines = int(args.get("lines", TMUX_DEFAULT_CAPTURE_LINES))

        cmd = _tmux("capture-pane", "-t", session_id, "-p", "-S", f"-{lines}")
        rc, stdout, stderr = await _run(cmd, timeout=10.0)
        if rc != 0:
            return {"session_id": session_id, "found": False, "error": stderr.strip()}

        meta = self._tmux_sessions.get(session_id, {})
        tail = stdout.rstrip("\n")
        return {
            "session_id": session_id,
            "found": True,
            "lines": tail,
            "line_count": tail.count("\n") + 1 if tail else 0,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "state": meta.get("state"),
        }

    async def _send_keys(self, args: dict[str, Any]) -> dict[str, Any]:
        """Send keystrokes to a tmux session.

        `keys`:
          - default (literal=True): plain text. \\n splits and inserts Enter between segments.
            Use 'y\\n' to grant CC's [y/N] permission prompt.
          - literal=False: tmux-named keys list (e.g. ['y', 'Enter'] or ['C-c']).
        """
        session_id = args.get("session_id") or args.get("session_name")
        keys = args.get("keys")
        literal = args.get("literal", True)
        if not session_id or keys is None:
            raise ValueError("claude_code.send_keys: 'session_id' and 'keys' required")

        meta = self._tmux_sessions.get(session_id)

        if literal:
            segments = keys.split("\n") if isinstance(keys, str) else [str(k) for k in keys]
            for i, segment in enumerate(segments):
                if segment:
                    rc, _, stderr = await _run(_tmux("send-keys", "-l", "-t", session_id, segment), timeout=5.0)
                    if rc != 0:
                        raise RuntimeError(f"tmux send-keys (literal) failed: {stderr.strip()}")
                if i < len(segments) - 1:
                    rc, _, stderr = await _run(_tmux("send-keys", "-t", session_id, "Enter"), timeout=5.0)
                    if rc != 0:
                        raise RuntimeError(f"tmux send-keys (Enter) failed: {stderr.strip()}")
        else:
            key_list = keys if isinstance(keys, list) else [keys]
            rc, _, stderr = await _run(_tmux("send-keys", "-t", session_id, *key_list), timeout=5.0)
            if rc != 0:
                raise RuntimeError(f"tmux send-keys (named) failed: {stderr.strip()}")

        if meta:
            meta["last_activity"] = datetime.now(timezone.utc).isoformat()
        return {"session_id": session_id, "sent": True, "literal": literal}

    async def _kill(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = args.get("session_id") or args.get("session_name")
        if not session_id:
            raise ValueError("claude_code.kill: 'session_id' required")
        # Stop the watcher first to avoid one last poll on a dying session.
        self._stop_watcher(session_id)
        cmd = _tmux("kill-session", "-t", session_id)
        rc, _, stderr = await _run(cmd, timeout=10.0)
        if rc != 0:
            return {"session_id": session_id, "killed": False, "reason": stderr.strip()}
        self._tmux_sessions.pop(session_id, None)
        return {"session_id": session_id, "killed": True}

    # ── Reverse-wake watcher (Chunk 2) ──

    def _start_watcher(self, session_id: str) -> None:
        if session_id in self._watchers and not self._watchers[session_id].done():
            return
        task = asyncio.create_task(self._watch_loop(session_id))
        self._watchers[session_id] = task

    def _stop_watcher(self, session_id: str) -> None:
        task = self._watchers.pop(session_id, None)
        if task and not task.done():
            task.cancel()

    async def _start_watcher_action(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = args.get("session_id")
        if not session_id:
            raise ValueError("claude_code.start_watcher: 'session_id' required")
        if self._event_pusher is None:
            return {"session_id": session_id, "started": False, "reason": "event_pusher not attached"}
        self._start_watcher(session_id)
        return {"session_id": session_id, "started": True}

    async def _stop_watcher_action(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = args.get("session_id")
        if not session_id:
            raise ValueError("claude_code.stop_watcher: 'session_id' required")
        self._stop_watcher(session_id)
        return {"session_id": session_id, "stopped": True}

    async def _watch_loop(self, session_id: str) -> None:
        """Background task: polls tmux pane, emits reverse-wake events on permission patterns.

        Survives transient capture failures; exits when the session disappears.
        Dedup logic: only emits one event per distinct pane-content match (so a stable
        permission prompt doesn't fire repeatedly each poll).
        """
        last_match_signature = ""
        consecutive_errors = 0
        while True:
            try:
                await asyncio.sleep(WATCHER_POLL_INTERVAL_S)
                pane_result = await self._get_pane({"session_id": session_id, "lines": TMUX_DEFAULT_CAPTURE_LINES})
                if not pane_result.get("found"):
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        # Session probably gone — exit watcher
                        break
                    continue
                consecutive_errors = 0
                pane = pane_result.get("lines", "")
                if not pane:
                    continue

                hit_kind, hit_match = self._classify_pane(pane)
                if hit_kind is None:
                    continue

                # Dedup on (kind, last ~120 chars of match context)
                match_snippet = (hit_match.group(0) if hasattr(hit_match, "group") else str(hit_match))[:120]
                signature = f"{hit_kind}:{match_snippet}"
                if signature == last_match_signature:
                    continue
                last_match_signature = signature

                # Build context: 5 lines around the match
                lines_list = pane.splitlines()
                ctx_lines: list[str] = []
                for i, line in enumerate(lines_list):
                    if hit_match.search(line) if isinstance(hit_match, re.Pattern) else (match_snippet[:50] in line):
                        start = max(0, i - 4)
                        ctx_lines = lines_list[start : i + 1]
                        break
                if not ctx_lines:
                    ctx_lines = lines_list[-6:]
                context = "\n".join(ctx_lines)

                payload: dict[str, Any]
                if hit_kind == "permission_request":
                    payload = {
                        "from_tool": "claude_code",
                        "wake_kind": "permission_request",
                        "context": context,
                        "session_id": session_id,
                        "options": [
                            {"id": "allow_once", "label": "Allow once"},
                            {"id": "allow_always", "label": "Always allow"},
                            {"id": "deny", "label": "Deny"},
                        ],
                    }
                elif hit_kind == "completion":
                    payload = {
                        "from_tool": "claude_code",
                        "wake_kind": "completion_notice",
                        "context": context,
                        "session_id": session_id,
                        "options": [],
                    }
                else:  # error
                    payload = {
                        "from_tool": "claude_code",
                        "wake_kind": "error",
                        "context": context,
                        "session_id": session_id,
                        "options": [],
                    }

                if self._event_pusher:
                    await self._event_pusher({
                        "kind": "tool_reverse_wake",
                        "payload": payload,
                    })
            except asyncio.CancelledError:
                break
            except Exception:
                # Don't let a single bad poll kill the watcher
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    break

    async def _test_inject_wake(self, args: dict[str, Any]) -> dict[str, Any]:
        """Test-only: synthesize a tool_reverse_wake event and push it to Cortex.

        Use args.wake_kind ('permission_request' default), args.session_id (default fake),
        args.context (default a plausible CC permission line).
        Returns whether the event was pushed.
        """
        if self._event_pusher is None:
            return {"pushed": False, "reason": "event_pusher not attached"}
        wake_kind = args.get("wake_kind", "permission_request")
        session_id = args.get("session_id", "cc-test00000")
        context = args.get("context") or (
            "I want to run Bash command: ls ~/Code/Projects/Constellation\n"
            "Do you want to allow this command? [y/N]"
        )
        options = (
            [
                {"id": "allow_once", "label": "Allow once"},
                {"id": "allow_always", "label": "Always allow"},
                {"id": "deny", "label": "Deny"},
            ]
            if wake_kind == "permission_request"
            else []
        )
        await self._event_pusher({
            "kind": "tool_reverse_wake",
            "payload": {
                "from_tool": "claude_code",
                "wake_kind": wake_kind,
                "context": context,
                "session_id": session_id,
                "options": options,
            },
        })
        return {"pushed": True, "wake_kind": wake_kind, "session_id": session_id}

    @staticmethod
    def _classify_pane(pane: str) -> tuple[str | None, Any]:
        # Search last 1500 chars (recent activity)
        recent = pane[-1500:]
        for pat in PERMISSION_PATTERNS:
            m = pat.search(recent)
            if m:
                return "permission_request", pat  # return pattern for context-line search
        for pat in ERROR_PATTERNS:
            m = pat.search(recent)
            if m:
                return "error", pat
        for pat in COMPLETION_PATTERNS:
            m = pat.search(recent)
            if m:
                return "completion", pat
        return None, None

    async def _list_tmux(self, args: dict[str, Any]) -> dict[str, Any]:
        """List adapter-tracked + actual tmux sessions on our socket."""
        cmd = _tmux("list-sessions", "-F", "#{session_name}")
        rc, stdout, _ = await _run(cmd, timeout=5.0)
        actual = [s.strip() for s in stdout.splitlines() if s.strip()] if rc == 0 else []
        for stale in list(self._tmux_sessions.keys()):
            if stale not in actual:
                self._tmux_sessions[stale]["state"] = "gone"
        return {
            "actual": actual,
            "tracked": list(self._tmux_sessions.values()),
        }

    # ── Streaming agent (AGENT-ARCHITECTURE-V2.md Phase 5a, v2 = tmux) ──────
    #
    # v2 deliberately avoids `claude -p` because that bills from a separate
    # API quota. Interactive TUI mode (regular `claude`) uses the user's
    # subscription. We get the same per-event visibility by TAILING the
    # session.jsonl file CC writes to ~/.claude/projects/<sanitized-cwd>/
    # <session-id>.jsonl — its format is essentially stream-json persisted.
    #
    # Mid-flight correction: send_keys the new user text into the tmux pane.

    async def _agent(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run CC as the agent for a complex task.

        Spawns a tmux-hosted interactive `claude` (uses user's subscription
        quota, NOT the -p API quota), sends the brief, tails CC's session
        .jsonl file as a stream-json equivalent, distills events to HUD
        progress, returns the final structured result.

        Args:
          brief:              full prompt for CC (caller inlines context)
          output_schema_hint: (optional) JSON Schema dict OR text description
                              of the required shape — appended to the brief so
                              CC's last assistant message contains parseable
                              JSON. (No --json-schema flag in TUI mode.)
          add_dirs:           paths to grant CC read access to
          working_dir:        cwd for CC (default $HOME)
          parent_event_id:    Cortex event id this run belongs to
          timeout_s:          total wall-clock cap (default 300s)
          idle_quiet_s:       seconds of jsonl silence after a final assistant
                              message that counts as "done" (default 6s)
        """
        brief = args.get("brief")
        if not brief:
            raise ValueError("claude_code.agent: 'brief' is required")
        output_schema_hint = args.get("output_schema_hint") or args.get("output_schema")
        add_dirs = args.get("add_dirs") or []
        working_dir = args.get("working_dir") or os.path.expanduser("~")
        parent_event_id = args.get("parent_event_id") or args.get("_event_id")
        timeout_s = float(args.get("timeout_s", 300.0))
        idle_quiet_s = float(args.get("idle_quiet_s", 6.0))

        # ── 1. Pre-allocate a session UUID so we know the jsonl path upfront ──
        cc_session_id = str(uuid.uuid4())
        # CC names project dirs from the REAL path (follows symlinks). On macOS
        # /tmp is symlinked to /private/tmp, so resolve before sanitising.
        real_cwd = Path(os.path.expanduser(working_dir)).resolve()
        sanitized = str(real_cwd).replace("/", "-")
        project_dir = Path.home() / ".claude" / "projects" / sanitized
        session_jsonl = project_dir / f"{cc_session_id}.jsonl"

        # Ensure target dir exists (CC may not create it if cwd is brand new)
        try:
            project_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        # output_schema_hint signals "we expect JSON, parse + validate the
        # final assistant text against this schema". Callers (Cortex's
        # _assemble_agent_brief) are now responsible for INCLUDING the
        # schema text inside the brief itself, in the right place (head)
        # for max model attention. Adapter no longer auto-appends to avoid
        # double-pasting and to give the caller full control of the layout.
        # Older callers that pass schema_hint without inlining will still
        # get "structured" parsing on the result; CC just may comply less
        # consistently without the inline contract — that's the caller's
        # responsibility now.

        # tmux session name is derived from cc_session_id; computed once + reused
        # so Cortex's feedback handler can route send-keys to the right session
        # without us having to coordinate state with it separately.
        tmux_session = f"{TMUX_SESSION_PREFIX}agent-{cc_session_id[:8]}"

        # Helpers
        n_progress_emitted = 0
        async def _emit_progress(progress: dict[str, Any]) -> None:
            nonlocal n_progress_emitted
            if not self._event_pusher or not parent_event_id:
                return
            await self._event_pusher({
                "kind": "agent_progress",
                "ts": datetime.now(timezone.utc).isoformat(),
                "payload": {
                    "parent_event_id": parent_event_id,
                    "agent_session_id": cc_session_id,
                    "tmux_session": tmux_session,
                    **progress,
                },
            })
            n_progress_emitted += 1

        await _emit_progress({"stage": "started", "icon": "🤖", "detail": "Agent online"})

        started_at = datetime.now(timezone.utc).isoformat()

        # ── 2. Launch claude in tmux with our session UUID ──
        # (tmux_session computed above so the started progress event already
        # carries it; reuse here without re-derivation)
        claude_cmd_parts = ["claude", "--session-id", cc_session_id]
        for d in add_dirs:
            claude_cmd_parts += ["--add-dir", os.path.expanduser(str(d))]
        # Quote each part for the shell command tmux will run
        import shlex as _shlex
        claude_cmd_str = " ".join(_shlex.quote(p) for p in claude_cmd_parts)
        tmux_new = _tmux(
            "new-session", "-d", "-s", tmux_session,
            "-c", working_dir,
            "-x", "200", "-y", "50",  # nicer pane width for readability
            claude_cmd_str,
        )
        rc, _, err = await _run(tmux_new, timeout=10)
        if rc != 0:
            await _emit_progress({"stage": "error", "detail": f"tmux new-session failed: {err[:200]}"})
            return {"ok": False, "error": f"tmux: {err.strip() or 'unknown'}", "session_id": cc_session_id}

        # Track in the adapter's session map (so /api/cc/sessions sees it)
        self._tmux_sessions[tmux_session] = {
            "session_name": tmux_session,
            "kind": "agent",
            "cc_session_id": cc_session_id,
            "started_at": started_at,
            "state": "running",
        }

        try:
            # Wait for CC to reach the prompt input area, then paste the brief.
            # CC TUI typically renders the input prompt within 2-3s.
            await asyncio.sleep(2.5)
            # Send the brief: tmux paste-buffer is the cleanest way to handle
            # multi-line text reliably (send-keys with newlines triggers send).
            buf_name = f"cb_{cc_session_id[:8]}"
            await _run(_tmux("set-buffer", "-b", buf_name, brief), timeout=5)
            await _run(_tmux("paste-buffer", "-b", buf_name, "-t", tmux_session), timeout=5)
            await _run(_tmux("delete-buffer", "-b", buf_name), timeout=5)
            # Press Enter to submit
            await asyncio.sleep(0.3)
            await _run(_tmux("send-keys", "-t", tmux_session, "Enter"), timeout=5)

            await _emit_progress({"stage": "brief_sent", "icon": "▶️", "detail": "task accepted, thinking…"})

            # ── 3. Tail the session.jsonl ──
            async def _heartbeat(msg: str) -> None:
                await _emit_progress({
                    "stage": "thinking",
                    "icon": "💭",
                    "detail": msg,
                })

            result = await _tail_jsonl_until_idle(
                session_jsonl,
                on_event=lambda ev: self._handle_jsonl_event(
                    ev, _emit_progress, cc_session_id,
                ),
                timeout_s=timeout_s,
                idle_quiet_s=idle_quiet_s,
                emit_heartbeat=_heartbeat,
            )

            last_assistant_text = result["last_assistant_text"]
            n_tool_uses = result["n_tool_uses"]
            terminate_reason = result["terminate_reason"]

            # ── 4. Parse final structured output from the last assistant text ──
            structured: Any = None
            parse_err: str | None = None
            if output_schema_hint:
                import re as _re
                txt = (last_assistant_text or "").strip()
                # Build candidate JSON strings to try, in priority order
                candidates: list[str] = []
                if txt:
                    candidates.append(txt)  # raw
                    fence_m = _re.search(r"```(?:json)?\s*\n?(.*?)```", txt, _re.DOTALL)
                    if fence_m:
                        candidates.append(fence_m.group(1).strip())
                    # Try to extract the {...} block if there's prose around it
                    brace_m = _re.search(r"(\{.*\})", txt, _re.DOTALL)
                    if brace_m:
                        candidates.append(brace_m.group(1).strip())
                for candidate in candidates:
                    try:
                        structured = json.loads(candidate)
                        parse_err = None
                        break
                    except json.JSONDecodeError as e:
                        parse_err = f"json parse failed: {e}"
                        continue

            # ── 5. Checkpoint detection — KEEP tmux alive if more phases follow ──
            is_checkpoint = (
                isinstance(structured, dict)
                and bool(structured.get("phase_done"))
                and bool((structured.get("next") or "").strip())
            )
            if not is_checkpoint:
                await _run(_tmux("kill-session", "-t", tmux_session), timeout=5)
                self._tmux_sessions.pop(tmux_session, None)
            else:
                # Mark the session as paused so /api/cc/sessions surfaces it
                self._tmux_sessions[tmux_session] = {
                    **(self._tmux_sessions.get(tmux_session) or {}),
                    "state": "paused_at_checkpoint",
                    "phase_summary": str(structured.get("summary") or "")[:200],
                }

            finished_at = datetime.now(timezone.utc).isoformat()

            await _emit_progress({
                "stage": "checkpoint" if is_checkpoint else "completed",
                "icon": "⏸" if is_checkpoint else "🎯",
                "detail": (
                    f"Phase pause — {(structured.get('summary') or 'phase done')[:80]}"
                    if is_checkpoint
                    else f"Agent done ({terminate_reason}); {n_tool_uses} tool uses, {len(result['events'])} events."
                ),
                "n_tool_uses": n_tool_uses,
            })

            return {
                "ok": (parse_err is None) if output_schema_hint else True,
                "session_id": cc_session_id,
                "tmux_session": tmux_session,
                "session_jsonl": str(session_jsonl),
                "started_at": started_at,
                "finished_at": finished_at,
                "n_tool_uses": n_tool_uses,
                "n_progress_emitted": n_progress_emitted,
                "n_events_seen": len(result["events"]),
                "terminate_reason": terminate_reason,
                "result_text": last_assistant_text,
                "structured": structured,
                "parse_error": parse_err,
                "is_checkpoint": is_checkpoint,   # True ⇒ caller must claude_code.agent_continue
            }
        except Exception as e:
            # Best-effort cleanup
            await _run(_tmux("kill-session", "-t", tmux_session), timeout=5)
            self._tmux_sessions.pop(tmux_session, None)
            await _emit_progress({"stage": "error", "detail": f"agent: {type(e).__name__}: {e}"})
            return {
                "ok": False, "error": f"{type(e).__name__}: {e}",
                "session_id": cc_session_id,
                "tmux_session": tmux_session,
            }

    # ── helper for jsonl streaming ─────────────────────────────────────────
    def _handle_jsonl_event(
        self,
        ev: dict[str, Any],
        emit: Callable[[dict[str, Any]], Awaitable[None]],
        cc_session_id: str,
    ) -> Awaitable[None] | None:
        """Distill ONE jsonl event + emit progress. Schedules a coroutine."""
        progress = _distill_jsonl_event(ev)
        if progress:
            return emit(progress)
        return None

    # ── Multi-phase resume + cleanup (Phase 5 v2.6) ─────────────────────────

    async def _agent_continue(self, args: dict[str, Any]) -> dict[str, Any]:
        """Resume a paused agent: send the user's response into its tmux pane
        and tail until the next end_turn / checkpoint.

        Args:
          tmux_session:        the tmux session name from a prior _agent return
          cc_session_id:       (optional, redundant) session UUID; used to
                               re-derive the jsonl path if not passed
          user_text:           what to send to CC. "continue" / "go" / "yes"
                               are normal proceed signals; anything else is a
                               redirection CC will integrate.
          parent_event_id:     same as _agent — for progress routing
          working_dir:         needed to derive jsonl path; same as the
                               original _agent call
          timeout_s, idle_quiet_s: same semantics as _agent
        """
        tmux_session = args.get("tmux_session")
        if not tmux_session:
            raise ValueError("claude_code.agent_continue: 'tmux_session' required")
        cc_session_id = args.get("cc_session_id") or args.get("session_id")
        if not cc_session_id:
            # Derive from tmux_session name (cc-agent-<first 8 of uuid>) — works
            # only if Cortex provided the original session_id back to us
            raise ValueError("claude_code.agent_continue: 'cc_session_id' required")
        user_text = args.get("user_text") or args.get("text") or "continue"
        parent_event_id = args.get("parent_event_id") or args.get("_event_id")
        working_dir = args.get("working_dir") or os.path.expanduser("~")
        timeout_s = float(args.get("timeout_s", 300.0))
        idle_quiet_s = float(args.get("idle_quiet_s", 6.0))

        # Re-derive jsonl path (same logic as _agent)
        real_cwd = Path(os.path.expanduser(working_dir)).resolve()
        sanitized = str(real_cwd).replace("/", "-")
        session_jsonl = Path.home() / ".claude" / "projects" / sanitized / f"{cc_session_id}.jsonl"

        # Verify session still alive
        rc, _, _ = await _run(_tmux("has-session", "-t", tmux_session), timeout=5)
        if rc != 0:
            return {"ok": False, "error": f"tmux session {tmux_session} no longer exists",
                    "session_id": cc_session_id, "tmux_session": tmux_session}

        n_progress_emitted = 0
        async def _emit_progress(progress: dict[str, Any]) -> None:
            nonlocal n_progress_emitted
            if not self._event_pusher or not parent_event_id:
                return
            await self._event_pusher({
                "kind": "agent_progress",
                "ts": datetime.now(timezone.utc).isoformat(),
                "payload": {
                    "parent_event_id": parent_event_id,
                    "agent_session_id": cc_session_id,
                    "tmux_session": tmux_session,
                    **progress,
                },
            })
            n_progress_emitted += 1

        await _emit_progress({
            "stage": "resumed", "icon": "▶️",
            "detail": f'resuming with: "{user_text[:80]}"',
        })

        # Find current jsonl size so we don't re-replay events from prior phase
        try:
            start_pos = session_jsonl.stat().st_size if session_jsonl.exists() else 0
        except OSError:
            start_pos = 0

        # Inject user_text via tmux paste-buffer (more reliable than send-keys
        # for multi-line) then Enter
        import shlex as _shlex  # noqa
        buf = f"cb_resume_{cc_session_id[:8]}"
        await _run(_tmux("set-buffer", "-b", buf, user_text), timeout=5)
        await _run(_tmux("paste-buffer", "-b", buf, "-t", tmux_session), timeout=5)
        await _run(_tmux("delete-buffer", "-b", buf), timeout=5)
        await asyncio.sleep(0.3)
        await _run(_tmux("send-keys", "-t", tmux_session, "Enter"), timeout=5)

        # Tail from current position
        async def _heartbeat(msg: str) -> None:
            await _emit_progress({"stage": "thinking", "icon": "💭", "detail": msg})

        result = await _tail_jsonl_until_idle_from(
            session_jsonl, start_pos=start_pos,
            on_event=lambda ev: self._handle_jsonl_event(ev, _emit_progress, cc_session_id),
            timeout_s=timeout_s, idle_quiet_s=idle_quiet_s,
            emit_heartbeat=_heartbeat,
        )

        # Parse structured output
        structured: Any = None
        parse_err: str | None = None
        last_text = result["last_assistant_text"] or ""
        if last_text.strip():
            import re as _re
            candidates = [last_text.strip()]
            fence_m = _re.search(r"```(?:json)?\s*\n?(.*?)```", last_text, _re.DOTALL)
            if fence_m:
                candidates.append(fence_m.group(1).strip())
            brace_m = _re.search(r"(\{.*\})", last_text, _re.DOTALL)
            if brace_m:
                candidates.append(brace_m.group(1).strip())
            for candidate in candidates:
                try:
                    structured = json.loads(candidate)
                    parse_err = None
                    break
                except json.JSONDecodeError as e:
                    parse_err = f"json parse failed: {e}"

        is_checkpoint = (
            isinstance(structured, dict)
            and bool(structured.get("phase_done"))
            and bool((structured.get("next") or "").strip())
        )
        if not is_checkpoint:
            await _run(_tmux("kill-session", "-t", tmux_session), timeout=5)
            self._tmux_sessions.pop(tmux_session, None)
        else:
            self._tmux_sessions[tmux_session] = {
                **(self._tmux_sessions.get(tmux_session) or {}),
                "state": "paused_at_checkpoint",
                "phase_summary": str((structured or {}).get("summary") or "")[:200],
            }

        await _emit_progress({
            "stage": "checkpoint" if is_checkpoint else "completed",
            "icon": "⏸" if is_checkpoint else "🎯",
            "detail": (
                f"Phase pause — {((structured or {}).get('summary') or 'phase done')[:80]}"
                if is_checkpoint
                else f"Agent done ({result['terminate_reason']}); {result['n_tool_uses']} tool uses."
            ),
            "n_tool_uses": result["n_tool_uses"],
        })

        return {
            "ok": parse_err is None,
            "session_id": cc_session_id,
            "tmux_session": tmux_session,
            "n_tool_uses": result["n_tool_uses"],
            "n_progress_emitted": n_progress_emitted,
            "terminate_reason": result["terminate_reason"],
            "result_text": last_text,
            "structured": structured,
            "parse_error": parse_err,
            "is_checkpoint": is_checkpoint,
        }

    async def _agent_kill(self, args: dict[str, Any]) -> dict[str, Any]:
        """Explicitly tear down a paused agent's tmux session (user cancelled)."""
        tmux_session = args.get("tmux_session")
        if not tmux_session:
            raise ValueError("claude_code.agent_kill: 'tmux_session' required")
        rc, _, _ = await _run(_tmux("kill-session", "-t", tmux_session), timeout=5)
        self._tmux_sessions.pop(tmux_session, None)
        return {"ok": rc == 0, "tmux_session": tmux_session}


# ── Session.jsonl tail + distillation (module-level helpers) ──────────────

async def _tail_jsonl_until_idle_from(
    path: Path,
    *,
    start_pos: int = 0,
    on_event,
    timeout_s: float,
    idle_quiet_s: float,
    poll_s: float = 0.4,
    heartbeat_s: float = 8.0,
    emit_heartbeat=None,
) -> dict[str, Any]:
    """Same as _tail_jsonl_until_idle but seeks to `start_pos` first. Used
    by agent_continue() so we only tail events emitted AFTER the resume
    point, not replay the prior phase's events."""
    return await _tail_jsonl_impl(
        path, start_pos=start_pos, on_event=on_event,
        timeout_s=timeout_s, idle_quiet_s=idle_quiet_s, poll_s=poll_s,
        heartbeat_s=heartbeat_s, emit_heartbeat=emit_heartbeat,
    )


async def _tail_jsonl_until_idle(
    path: Path,
    *,
    on_event,    # callable(dict) -> Awaitable | None
    timeout_s: float,
    idle_quiet_s: float,
    poll_s: float = 0.4,
    heartbeat_s: float = 8.0,    # emit "still thinking…" after this much silence
    emit_heartbeat=None,         # async (msg: str) -> None
) -> dict[str, Any]:
    """Original entry — tails from start of file. Convenience wrapper."""
    return await _tail_jsonl_impl(
        path, start_pos=0, on_event=on_event,
        timeout_s=timeout_s, idle_quiet_s=idle_quiet_s, poll_s=poll_s,
        heartbeat_s=heartbeat_s, emit_heartbeat=emit_heartbeat,
    )


async def _tail_jsonl_impl(
    path: Path,
    *,
    start_pos: int = 0,
    on_event,
    timeout_s: float,
    idle_quiet_s: float,
    poll_s: float = 0.4,
    heartbeat_s: float = 8.0,
    emit_heartbeat=None,
) -> dict[str, Any]:
    """Tail a CC session.jsonl until CC ends its turn (best signal) OR we
    exceed quiet/timeout limits.

    Completion ladder (preferred → fallback):
      1. saw an assistant message with `stop_reason == "end_turn"`
         (CC explicitly closed its response — the cleanest signal)
      2. saw_assistant_text=True AND no new events for `idle_quiet_s`
         (CC produced prose but didn't tag end_turn; common in cancelled or
         partial outputs)
      3. `timeout_s` hard limit hit

    Heartbeat: Opus extended-thinking does NOT stream thinking deltas to the
    session.jsonl file — CC writes nothing while thinking. To the HUD this
    looks like a stall. If `emit_heartbeat` is given, this loop calls it
    every `heartbeat_s` of jsonl silence so the HUD can show "still thinking"
    instead of going dead.

    Returns:
      events, last_assistant_text, n_tool_uses, terminate_reason
    """
    loop = asyncio.get_event_loop()
    started = loop.time()
    deadline = started + timeout_s

    events: list[dict[str, Any]] = []
    last_assistant_text: str | None = None
    n_tool_uses = 0
    last_change = started
    last_heartbeat_at = started
    pos = start_pos  # byte offset into the jsonl file
    saw_assistant_text = False
    saw_end_turn = False
    partial = ""

    while loop.time() < deadline:
        if not path.exists():
            await asyncio.sleep(poll_s)
            if loop.time() - started > 10 and not events:
                return {
                    "events": events, "last_assistant_text": None,
                    "n_tool_uses": 0, "terminate_reason": "file_missing",
                }
            continue

        try:
            with path.open("rb") as f:
                f.seek(pos)
                chunk = f.read().decode("utf-8", errors="replace")
                pos = f.tell()
        except OSError:
            await asyncio.sleep(poll_s)
            continue

        if chunk:
            partial += chunk
            *lines, partial = partial.split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append(ev)
                last_change = asyncio.get_event_loop().time()

                if ev.get("type") == "assistant":
                    msg = ev.get("message") or {}
                    # Track stop_reason on the snapshot; the message ID can
                    # appear in multiple events as content streams in, but
                    # only the FINAL snapshot has stop_reason set to a
                    # non-null value.
                    if msg.get("stop_reason") == "end_turn":
                        saw_end_turn = True
                    for c in msg.get("content") or []:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "text" and (c.get("text") or "").strip():
                            saw_assistant_text = True
                            last_assistant_text = c["text"]
                        elif c.get("type") == "tool_use":
                            n_tool_uses += 1

                coro = on_event(ev)
                if coro is not None:
                    try:
                        await coro
                    except Exception:
                        pass

        # ── Completion signals, in priority order ──
        if saw_end_turn:
            # Give one extra poll tick for any trailing event to settle
            await asyncio.sleep(poll_s)
            return {
                "events": events, "last_assistant_text": last_assistant_text,
                "n_tool_uses": n_tool_uses, "terminate_reason": "end_turn",
            }

        if saw_assistant_text and (loop.time() - last_change) >= idle_quiet_s:
            return {
                "events": events, "last_assistant_text": last_assistant_text,
                "n_tool_uses": n_tool_uses, "terminate_reason": "idle_after_text",
            }

        # ── Heartbeat: extended-thinking opacity ──
        # Opus doesn't stream thinking deltas to disk while thinking. If we've
        # been quiet for `heartbeat_s`, emit a "still thinking" beacon so the
        # HUD doesn't look like the agent died. Subsequent heartbeats every
        # `heartbeat_s` seconds until something else happens.
        now = loop.time()
        if (
            emit_heartbeat is not None
            and (now - last_change) >= heartbeat_s
            and (now - last_heartbeat_at) >= heartbeat_s
        ):
            elapsed_quiet = int(now - last_change)
            try:
                await emit_heartbeat(f"still thinking… ({elapsed_quiet}s quiet)")
            except Exception:
                pass
            last_heartbeat_at = now

        await asyncio.sleep(poll_s)

    return {
        "events": events, "last_assistant_text": last_assistant_text,
        "n_tool_uses": n_tool_uses, "terminate_reason": "timeout",
    }


def _distill_jsonl_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Distill ONE CC session.jsonl event → user-facing progress message.

    The jsonl format has top-level `type` in {user, assistant, system,
    queue-operation, file-history-snapshot, ai-title, last-prompt, attachment}.
    Only user/assistant carry actionable content; the rest are CC internals.
    """
    t = ev.get("type")

    # Defensive helper — content may be a list[dict] (normal) OR a bare string
    # (older CC sessions / certain message shapes). Skip cleanly if not list.
    def _content_blocks(msg: dict[str, Any]) -> list[dict[str, Any]]:
        c = msg.get("content")
        return c if isinstance(c, list) else []

    if t == "assistant":
        for c in _content_blocks(ev.get("message") or {}):
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "tool_use":
                name = c.get("name", "tool")
                icon, glance = _tool_glance(name, c.get("input") or {})
                return {
                    "stage": "tool_call",
                    "tool": name,
                    "icon": icon,
                    "detail": glance,           # HUD-friendly one-liner ≤ 80c
                }
            if ct == "text":
                text = (c.get("text") or "").strip()
                if not text:
                    return None
                # Keep assistant prose terse for HUD; show as a "thinking" line
                return {
                    "stage": "assistant_text",
                    "icon": "💭",
                    "detail": text[:120] + ("…" if len(text) > 120 else ""),
                }
            if ct == "thinking":
                return None
        return None

    if t == "user":
        for c in _content_blocks(ev.get("message") or {}):
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_result":
                content = c.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for x in content:
                        if isinstance(x, dict):
                            parts.append(str(x.get("text", x)))
                        else:
                            parts.append(str(x))
                    content = " ".join(parts)
                is_err = bool(c.get("is_error"))
                summary = _summarise_tool_result(str(content), is_err)
                return {
                    "stage": "tool_result",
                    "icon": "✗" if is_err else "✓",
                    "detail": summary,
                    "is_error": is_err,
                }
        return None

    return None


# ── Stream-json distillation (legacy -p mode, kept for reference) ─────────

def _distill_progress(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one CC stream-json event into a user-facing HUD progress message.

    Returns None to skip noisy / internal events (thinking, partial token
    deltas, content_block_stop, etc.). The signal we care about:
      - which tool CC is invoking (Bash command, Read path, Write path, etc.)
      - which tool result came back (truncated)
      - any assistant text the user might want to see (also truncated)
    """
    t = ev.get("type")

    if t == "assistant":
        msg = ev.get("message") or {}
        for c in msg.get("content", []) or []:
            ct = c.get("type")
            if ct == "tool_use":
                tool_name = c.get("name", "tool")
                inp = c.get("input") or {}
                detail = _tool_input_to_detail(tool_name, inp)
                return {
                    "stage": "tool_call",
                    "detail": detail,
                    "tool": tool_name,
                }
            if ct == "text":
                text = (c.get("text") or "").strip()
                if not text:
                    return None
                return {
                    "stage": "assistant_text",
                    "detail": text[:400],
                }
        return None

    if t == "user":
        # Tool result returning to CC
        msg = ev.get("message") or {}
        for c in msg.get("content", []) or []:
            if c.get("type") == "tool_result":
                content = c.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for x in content:
                        if isinstance(x, dict):
                            parts.append(str(x.get("text", x)))
                        else:
                            parts.append(str(x))
                    content = " ".join(parts)
                is_err = bool(c.get("is_error"))
                snippet = str(content).replace("\n", " ⏎ ")[:240]
                return {
                    "stage": "tool_result",
                    "detail": (f"ERROR: {snippet}" if is_err else snippet),
                    "is_error": is_err,
                }
        return None

    # Most stream_event sub-events are noisy (per-token deltas, block start/stop)
    # — skip. The assistant + user envelopes above carry the signal.
    return None


def _shorten(s: str, n: int = 70) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _basename_path(p: str) -> str:
    """Just show the filename or last 2 path components — HUD friendly."""
    if not p:
        return "?"
    parts = p.split("/")
    if len(parts) <= 2:
        return p
    return ".../" + "/".join(parts[-2:])


def _tool_glance(tool: str, inp: dict[str, Any]) -> tuple[str, str]:
    """Return (emoji, ≤70-char glanceable label) for one tool invocation.

    Designed for AR-glass HUD where vertical space is ~5 lines and you must
    grok the agent's action in <0.5s.  Prefer Anthropic's own short
    `description` arg when CC provides it; else extract the essence.
    """
    # CC's Bash tool requires a `description` (5-10 word verb phrase). Prefer it.
    if tool == "Bash":
        desc = inp.get("description") or ""
        if desc:
            return "🔧", _shorten(desc, 70)
        cmd = inp.get("command", "")
        # Pull leading executable for a hint
        first = (cmd.split() or ["?"])[0]
        return "🔧", _shorten(f"{first}: {cmd}", 70)

    if tool == "Read":
        return "📖", "reading " + _basename_path(inp.get("file_path") or inp.get("path") or "?")
    if tool == "Write":
        return "✍️", "writing " + _basename_path(inp.get("file_path") or inp.get("path") or "?")
    if tool == "Edit":
        return "📝", "editing " + _basename_path(inp.get("file_path") or inp.get("path") or "?")
    if tool == "Glob":
        return "🔍", _shorten(f"glob {inp.get('pattern','?')}", 70)
    if tool == "Grep":
        return "🔍", _shorten(f"grep '{inp.get('pattern') or inp.get('query') or '?'}'", 70)
    if tool == "WebFetch":
        url = inp.get("url") or "?"
        return "🌐", _shorten(f"fetching {url}", 70)
    if tool == "WebSearch":
        return "🌐", _shorten(f"searching: {inp.get('query') or '?'}", 70)
    if tool == "TodoWrite":
        return "📋", "updating own todo list"
    if tool == "TodoRead":
        return "📋", "checking own todos"
    if tool == "Task":
        return "🧵", _shorten(f"spawning sub-agent: {inp.get('subagent_type','?')}", 70)
    if tool == "StructuredOutput":
        return "📦", "finalising structured result"

    # MCP / extension tools — name has dots/colons
    if "__" in tool or ":" in tool:
        return "🔌", _shorten(tool, 70)

    # Generic fallback — show tool name + first input value
    if inp:
        k, v = next(iter(inp.items()))
        return "·", _shorten(f"{tool}({k}={v})", 70)
    return "·", _shorten(tool, 70)


def _summarise_tool_result(content: str, is_err: bool) -> str:
    """Compress a tool result down to a glanceable line. Bash output is the
    most variable — for short outputs show as-is, for long outputs show line
    count + first line."""
    if is_err:
        return _shorten(content.replace("\n", " ⏎ "), 70)
    text = content.strip()
    if not text:
        return "(no output)"
    lines = text.split("\n")
    if len(lines) == 1:
        return _shorten(lines[0], 70)
    if len(lines) <= 3:
        return _shorten(" ⏎ ".join(lines), 70)
    return _shorten(f"{len(lines)} lines · 1st: {lines[0]}", 70)


# Legacy alias for the old -p path (kept until 5g cleanup retires it)
def _tool_input_to_detail(tool: str, inp: dict[str, Any]) -> str:
    icon, label = _tool_glance(tool, inp)
    return f"{icon} {label}"
