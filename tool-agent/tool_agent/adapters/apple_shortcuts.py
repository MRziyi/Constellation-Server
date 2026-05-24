"""apple_shortcuts adapter — invoke any user-defined Apple Shortcut.

Actions: list, run.

Massive leverage: Zack's existing Shortcuts library becomes Cortex-callable. Each Shortcut
is essentially an Apple-blessed mini-tool — the user already defined behavior + UI in
Shortcuts.app; Cortex just dispatches by name.

Side-effects: `list`=none, `run`=highly variable (depends on what the Shortcut does).

Confirm: `run` is preview-always by default per confirm-policies "*" fallback. Specific
shortcuts user trusts can be downgraded to `auto` in confirm-policies.md by name pattern.

Mechanism:
  - `shortcuts list`  → newline-separated Shortcut names
  - `shortcuts run "Name" -i <input>` or `--input-path -` (we use stdin)
  - Output goes to stdout
"""

from __future__ import annotations

import asyncio
from typing import Any


async def _run_shortcuts(args_list: list[str], input_text: str | None = None, timeout: float = 30.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "shortcuts", *args_list,
        stdin=asyncio.subprocess.PIPE if input_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=input_text.encode("utf-8") if input_text else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", f"shortcut timed out after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


class AppleShortcutsAdapter:
    name = "apple_shortcuts"

    SIDE_EFFECT_ACTIONS = {"run"}

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        if action in self.SIDE_EFFECT_ACTIONS and result_format == "draft":
            raise ValueError(
                f"apple_shortcuts.{action} has no draft semantics — a Shortcut either runs or doesn't."
            )

        if action == "list":
            return await self._list(args)
        if action == "run":
            return await self._run(args)
        raise ValueError(f"apple_shortcuts: unknown action '{action}'")

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        folder = args.get("folder")
        cmd_args = ["list"]
        if folder:
            cmd_args.extend(["--folder-name", folder])
        rc, stdout, stderr = await _run_shortcuts(cmd_args, timeout=5.0)
        if rc != 0:
            raise RuntimeError(f"shortcuts list failed (rc={rc}): {stderr.strip()}")
        names = [line.strip() for line in stdout.splitlines() if line.strip()]
        return {"folder": folder, "count": len(names), "shortcuts": names}

    async def _run(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("name") or args.get("shortcut")
        if not name:
            raise ValueError("apple_shortcuts.run: 'name' required")
        input_text = args.get("input")
        timeout = float(args.get("timeout_s", 30.0))

        cmd_args = ["run", name]
        if input_text is not None:
            cmd_args.append("--input-path")
            cmd_args.append("-")  # stdin

        rc, stdout, stderr = await _run_shortcuts(cmd_args, input_text=input_text, timeout=timeout)
        if rc != 0:
            raise RuntimeError(f"shortcut '{name}' failed (rc={rc}): {stderr.strip() or stdout.strip()}")
        return {
            "name": name,
            "rc": rc,
            "output": stdout,
            "stderr": stderr.strip() if stderr.strip() else None,
        }
