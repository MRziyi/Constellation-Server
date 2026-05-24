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
