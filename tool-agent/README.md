# Tool Agent

The hands of Constellation. Runs as a Python `asyncio` service on Mac mini, listens for RPC dispatches from Cortex on `localhost:8889`, executes Mac-local tools through adapters, returns results.

## Status

🔄 **Phase 1 spine** — see [HANDOFF.md §6](../HANDOFF.md).

Currently: WebSocket server + Tool Registry + `echo` adapter (Phase 1 stub). Real adapters (`claude_code` / `applescript_*` / `fs`) come in Phase 2.

## Quick start (dev)

```bash
cd tool-agent/
python3 -m venv .venv
.venv/bin/pip install -e .

.venv/bin/python -m tool_agent.main
```

Tool Agent must start before Cortex tries to dispatch.

## Module layout

```
tool-agent/
├── pyproject.toml
├── adapters.yaml             ← which adapters to load (Phase 1: echo only)
├── tool_agent/
│   ├── __init__.py
│   ├── main.py               ← CLI entry point
│   ├── server.py             ← WSS server on localhost:8889
│   ├── registry.py           ← loads adapters per adapters.yaml
│   └── adapters/
│       ├── __init__.py       ← ToolAdapter Protocol
│       └── echo.py           ← Phase 1 stub
└── launchd/
    └── com.constellation.tool-agent.plist
```

## Adding a real adapter (Phase 2 onward)

1. Implement the class in `tool_agent/adapters/{name}.py`. Must implement `name: str` + `async def dispatch(action, args, context_pack, result_format) -> dict`.
2. Enable in `adapters.yaml`.
3. Restart Tool Agent.
4. Document the actions in [TOOL-ADAPTERS.md](../TOOL-ADAPTERS.md).

## Reference

- [COMPONENT-DESIGN.md §2](../COMPONENT-DESIGN.md) — Tool Agent design
- [TOOL-ADAPTERS.md](../TOOL-ADAPTERS.md) — adapter catalog
- [INTERFACE-CONTRACTS.md §3](../INTERFACE-CONTRACTS.md) — RPC schemas
- [HANDOFF.md](../HANDOFF.md) — current state + what to do next
