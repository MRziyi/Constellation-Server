"""Stable ID generation per INTERFACE-CONTRACTS.md §6.

Prefixes: evt_* event, cmd_* command, rpc_* RPC, rcpt_* receipt, pulse_* P6.
"""

import secrets


def _new(prefix: str) -> str:
    # 16 hex chars = 64 bits of entropy; enough for non-colliding ids.
    return f"{prefix}_{secrets.token_hex(8)}"


def event_id() -> str:
    return _new("evt")


def command_id() -> str:
    return _new("cmd")


def rpc_id() -> str:
    return _new("rpc")


def receipt_id() -> str:
    return _new("rcpt")


def pulse_id() -> str:
    return _new("pulse")


def session_id() -> str:
    # HUD sessions (conversation threads). Persisted JSONL keyed by this id.
    return _new("ses")
