# Cortex — the brain

The reasoning half of [Constellation Server](../README.md). A Python `asyncio` service that
receives events from the glasses, works out what you meant, executes the work, and writes
what happened back to the Digital Twin.

Everything here is orchestration and judgement. Actually touching your machine is
[Tool Agent](../tool-agent/)'s job.

## Interfaces

| Surface | Address | Who talks to it |
|---|---|---|
| Glass WSS | `:8888` | the eyewear client (or `test-harness/`) |
| Management HTTP | `:8890` | ops scripts, the web console, `/api/health` |
| Tool Agent client | `ws://127.0.0.1:8889` | outbound — Cortex dials Tool Agent |

Bind addresses default to loopback. `--host` / `--http-host` open them up; the launchd
template takes the value from `CORTEX_HOST` at install time.

## A turn, end to end

1. **`audio_chunk` events** accumulate in `audio_buffer.py` and stream into
   `whisper_pipeline.py`, which runs whisper.cpp locally in two tiers — a fast `base` pass
   for the live partial, a `small` pass for the final text.
2. **STT review.** The final transcript goes to the HUD as a card. Nothing acts on speech
   you have not confirmed. This gate is not optional.
3. **`classifier.py`** makes one cheap call: SIMPLE or COMPLEX. It is deliberately a tiny
   prompt — its output is one bit plus a short reason for telemetry. Running the full
   planner to answer "battery?" costs two orders of magnitude more for the same answer.
4. **SIMPLE → `router.py`**: plan a single adapter dispatch and send it to Tool Agent.
   **COMPLEX → `claude_sdk_agent.py`**: an in-process Claude Agent SDK run, given the brief
   from `agent_brief.py`, with the Twin as its working directory and phase checkpoints where
   you can steer.
5. **Side effects preview.** Sending, messaging, scheduling, or writing outside the Twin
   produces a preview card before it happens. Reads never do.
6. **Receipts.** `twin.py` appends what was done to `receipts/<date>.md` and every Twin
   write to `CHANGELOG.md`.

## Modules worth knowing

| File | Why it exists |
|---|---|
| `prompts.py` | every tunable prompt and model constant, in one place. Change behaviour here first. |
| `agent_brief.py` | single source of truth for what the agent is told. If the agent misbehaves, the bug is usually here. |
| `llm_cache.py` | the one chokepoint every LLM call passes through: caching, retry, tolerant JSON parsing, cost telemetry. |
| `twin.py` | Twin reads and writes, with mtime conflict handling — your hand edits win. |
| `face_index.py` | on-device face recognition (InsightFace + CoreML). Embeddings never leave the machine. |
| `enroll_parser.py` | turns a spoken introduction into a structured person record. |
| `insight_engine.py` | proactive surfacing. Off unless `CONSTELLATION_INSIGHT_ENGINE=1`, because it pushes cards you did not ask for. |
| `distiller.py` | converts implicit feedback (corrections, overrides, repetition) into Twin updates. |
| `sessions.py` · `session_router.py` · `session_browser.py` | conversation threads, and browsing past Claude Code sessions by voice. |
| `mail_inbound.py` | inbound mail becomes a HUD card you can long-press and dictate a threaded reply to. |
| `markdown_runs.py` | Markdown into the styled-run format the HUD renders. |
| `tcc_check.py` | reports which macOS permissions are missing, at startup rather than at first failure. |
| `schema.py` · `ids.py` | Pydantic wire models and ID generation, per INTERFACE-CONTRACTS.md. |

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -e .

# Tool Agent first — Cortex dials it.
(cd ../tool-agent && .venv/bin/python -m tool_agent.main) &
.venv/bin/python -m cortex.main
```

In practice use the ops scripts: `../scripts/start.sh`, `../scripts/status.sh`,
`../scripts/restart.sh cortex`, `../scripts/logs.sh`.

Configuration is environment variables — see [`../.env.example`](../.env.example).

## Design references

Design documents live in the [Constellation](https://github.com/MRziyi/Constellation) repository:

- [AGENT-ARCHITECTURE-V2.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/AGENT-ARCHITECTURE-V2.md) — the agent runtime. Read first.
- [COMPONENT-DESIGN.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/COMPONENT-DESIGN.md) — Cortex internals.
- [INTERFACE-CONTRACTS.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/INTERFACE-CONTRACTS.md) — wire schemas.
- [PROMPT-DESIGN-V2.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/PROMPT-DESIGN-V2.md) — prompt architecture and two-pass Twin loading.
- [DATA-MODEL.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/DATA-MODEL.md) — the Twin data model.
