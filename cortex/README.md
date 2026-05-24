# Cortex Agent

The brain of Constellation. Runs as a Python `asyncio` service on Mac mini, receives events from the Glass client (or the test-harness simulating one), routes via GPT, dispatches to Tool Agent, writes receipts to the Digital Twin.

## Status

🔄 **Phase 1 spine** — see [HANDOFF.md §6](../HANDOFF.md).

Currently: stub Router (echo) + minimum-viable preview/confirm + receipt writing + CHANGELOG appending. No GPT call yet. No real tool adapters yet (just dummy echo).

## Quick start (dev)

```bash
cd cortex/
python3 -m venv .venv
.venv/bin/pip install -e .

# In a separate terminal, start tool-agent first (Cortex connects to it lazily).
# Then:
.venv/bin/python -m cortex.main
```

## Module layout

```
cortex/
├── pyproject.toml
├── cortex/
│   ├── __init__.py
│   ├── main.py         ← CLI entry point (click)
│   ├── server.py       ← WSS server (Glass) + Tool Agent client + orchestration
│   ├── router.py       ← stub Router; Phase 2 calls GPT
│   ├── twin.py         ← Twin reader/writer + CHANGELOG appender
│   ├── schema.py       ← Pydantic models per INTERFACE-CONTRACTS.md
│   └── ids.py          ← evt_*/cmd_*/rpc_*/rcpt_*/pulse_* generators
└── launchd/
    └── com.constellation.cortex.plist
```

## Reference

- [COMPONENT-DESIGN.md §1](../COMPONENT-DESIGN.md) — Cortex Agent design
- [CORTEX-ROUTER-PROMPT.md](../CORTEX-ROUTER-PROMPT.md) — Phase 2 prompt
- [INTERFACE-CONTRACTS.md](../INTERFACE-CONTRACTS.md) — wire schemas
- [HANDOFF.md](../HANDOFF.md) — current state + what to do next
