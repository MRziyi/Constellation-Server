# Tool Agent — the hands

The executing half of [Constellation Server](../README.md). A Python `asyncio` service that
listens on `127.0.0.1:8889` for RPC dispatches from [Cortex](../cortex/), runs one leaf
action through one adapter, and returns the result.

**It does not reason.** No model calls, no planning, no deciding what to do next — that is
Cortex's job. Tool Agent is a registry of small, auditable adapters, which is what makes
"what can this system actually do to my machine" a question you can answer by reading one
directory.

It binds loopback only. Nothing outside the machine can reach it.

## The capability surface

[`adapters.yaml`](adapters.yaml) is the boundary. An adapter that is not listed and enabled
there cannot be reached, no matter what a model asks for. It is also what gets presented to
Cortex as the available tool set — so the file is both the allowlist and the menu.

| Adapter | Actions |
|---|---|
| `claude_code` | draft · run · continue · list_sessions — via the `claude` CLI |
| `applescript_mail` | read · search · draft · send (send always previews first) |
| `applescript_calendar` | list · create events |
| `applescript_reminders` | list · create reminders |
| `apple_notes` | read · write Notes.app |
| `apple_shortcuts` | invoke any user-defined Apple Shortcut |
| `imessage` | send via AppleScript · list_recent from `chat.db` (needs Full Disk Access) |
| `safari_state` | current tab · all tabs · recent history (history needs Full Disk Access) |
| `fs` | read · write · append · grep · list · delete — writes restricted to allowlisted roots |
| `system_status` | battery · focus · foreground app · network · time |
| `twin_query` | semantic Q&A over the Twin (grep plus synthesis) |
| `echo` | spine test; harmless, useful when something is broken |

There is deliberately no image-to-text adapter. A camera frame travels into the planner as
a multimodal image block and the model reads it directly, rather than being flattened into
a lossy text description first.

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m tool_agent.main
```

Start it before Cortex — Cortex dials out to it. In practice use `../scripts/start.sh`,
which orders them correctly.

## Adding an adapter

1. Write `tool_agent/adapters/<name>.py` with a class exposing `name: str` and
   `async def dispatch(action, args, context_pack, result_format) -> dict`.
2. Add it to [`adapters.yaml`](adapters.yaml) with `enabled: true`.
3. Restart Tool Agent.
4. Document its actions in
   [TOOL-ADAPTERS.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/TOOL-ADAPTERS.md).

Keep adapters dumb. If you find yourself wanting a model call inside one, the logic belongs
in Cortex.

Anything with a side effect must be previewable before it fires — that contract is enforced
by Cortex, but an adapter that cannot describe what it is about to do breaks it.

## Design references

- [COMPONENT-DESIGN.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/COMPONENT-DESIGN.md) — Tool Agent design.
- [TOOL-ADAPTERS.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/TOOL-ADAPTERS.md) — the adapter catalog.
- [INTERFACE-CONTRACTS.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/INTERFACE-CONTRACTS.md) — RPC schemas.
