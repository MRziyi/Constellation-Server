"""system_status adapter — Mac state snapshot for context-aware routing.

Single action: `get()` returns a dict of:
  - battery_pct  : int 0-100 (None if no battery / on iMac)
  - on_ac        : bool (true if plugged in / charging)
  - focus_mode   : str ('off' | 'do_not_disturb' | 'work' | 'personal' | etc.) — best effort
  - frontmost_app: str (current frontmost app name)
  - wifi_ssid    : str | None
  - tailscale    : bool (true if Tailscale is up)
  - now_iso      : ISO 8601 datetime in local tz
  - tz           : IANA tz id

Side-effects: none (all reads). Confirm policy: auto.

Use this to enrich Cortex Router's USER STATE block. Example consumers:
  - "Don't push P6 pulses when focus_mode is 'do_not_disturb'"
  - "Prefer SMS over email if frontmost_app is 'Maps' (commuting)"
  - "Defer image-heavy vision when battery_pct < 20"
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any


async def _run(cmd: list[str], timeout: float = 3.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        return -1, ""
    except FileNotFoundError:
        return -1, ""


async def _battery() -> tuple[int | None, bool]:
    rc, out = await _run(["pmset", "-g", "batt"])
    if rc != 0:
        return None, False
    # "InternalBattery-0 (id=...)\t72%; charging; ..."  OR  "AC Power"
    m = re.search(r"(\d+)%", out)
    pct = int(m.group(1)) if m else None
    on_ac = "AC Power" in out or "charging" in out.lower() or "charged" in out.lower()
    return pct, on_ac


async def _focus_mode() -> str:
    # macOS 12+ Focus state isn't trivially queryable; we approximate by checking
    # Do Not Disturb via `defaults read com.apple.ncprefs` JSON. Best effort.
    rc, out = await _run(
        ["defaults", "-currentHost", "read", "com.apple.controlcenter", "FocusModes"],
        timeout=2.0,
    )
    if rc == 0 and "active" in out.lower():
        # Try to extract a focus name
        m = re.search(r'"name"\s*=\s*"([^"]+)"', out)
        return m.group(1) if m else "active"
    return "off"


async def _frontmost_app() -> str:
    rc, out = await _run(["osascript", "-e", 'tell application "System Events" to get name of first application process whose frontmost is true'])
    return out.strip() if rc == 0 else "unknown"


async def _wifi_ssid() -> str | None:
    # macOS removed `airport` in newer versions; try networksetup as fallback.
    rc, out = await _run(["networksetup", "-getairportnetwork", "en0"], timeout=2.0)
    if rc == 0:
        # "Current Wi-Fi Network: NetworkName"
        m = re.search(r":\s*(.+)", out.strip())
        if m and "not associated" not in m.group(1).lower():
            return m.group(1).strip()
    return None


async def _tailscale_up() -> bool:
    rc, out = await _run(["tailscale", "status", "--json"], timeout=2.0)
    if rc != 0:
        return False
    return '"BackendState":"Running"' in out


class SystemStatusAdapter:
    name = "system_status"

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        if action != "get":
            raise ValueError(f"system_status: unknown action '{action}' (only 'get')")

        # Gather concurrently — each can independently fail without blocking.
        battery_task = asyncio.create_task(_battery())
        focus_task = asyncio.create_task(_focus_mode())
        app_task = asyncio.create_task(_frontmost_app())
        wifi_task = asyncio.create_task(_wifi_ssid())
        ts_task = asyncio.create_task(_tailscale_up())

        battery_pct, on_ac = await battery_task
        focus_mode = await focus_task
        frontmost = await app_task
        wifi = await wifi_task
        tailscale = await ts_task

        now = datetime.now().astimezone()
        return {
            "battery_pct": battery_pct,
            "on_ac": on_ac,
            "focus_mode": focus_mode,
            "frontmost_app": frontmost,
            "wifi_ssid": wifi,
            "tailscale": tailscale,
            "now_iso": now.isoformat(),
            "tz": str(now.tzinfo) if now.tzinfo else None,
        }
