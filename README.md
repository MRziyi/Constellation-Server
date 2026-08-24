# Constellation Server

**English** · [简体中文](README.zh-CN.md)

The Mac-side runtime of [Constellation](https://github.com/MRziyi/Constellation) — a
personal AI framework for all-day wearable assistance. This repository holds the two
daemons that do the thinking and the doing, plus the seed of the Markdown knowledge base
they read and write.

> **Scope.** This is a research prototype in daily single-user use, not a product. It runs
> on macOS, drives Apple apps through AppleScript, and has its author's name written into
> its system prompts. Read [Known limitations](#known-limitations) before you try to deploy it.

## The two daemons

| | **Cortex** — the brain | **Tool Agent** — the hands |
|---|---|---|
| Runs | `python -m cortex.main` | `python -m tool_agent.main` |
| Listens | WSS `:8888` (glasses) · HTTP `:8890` (management) | WS `127.0.0.1:8889` (Cortex only) |
| Does | STT, intent classification, planning, agent execution, Twin writes | executes one leaf action per RPC through an adapter |
| Talks to | OpenAI / Groq / the Claude Agent SDK, and Tool Agent | macOS apps, the filesystem, the Claude Code CLI |

Cortex is the only component that reasons. Tool Agent deliberately does not: it is a
registry of small, auditable adapters, so that "what can this system actually do to my
machine" is answerable by reading one directory.

## Request path

```
 glasses ──audio chunks──▶ whisper_pipeline ──▶ STT review card ──▶ (you approve)
                                                                        │
                                                                        ▼
                                                                   classifier
                                                       SIMPLE ◀───────┴───────▶ COMPLEX
                                                          │                       │
                                                     router.py                claude_sdk_agent
                                                (one adapter dispatch)     (multi-step, phase
                                                          │                 checkpoints, Twin
                                                          ▼                 as working dir)
                                                     Tool Agent ◀───────────────┘
                                                          │
                                              side effect? ──▶ preview card ──▶ (you approve)
                                                          │
                                                          ▼
                                                    execute · receipt · HUD card
```

Two gates are load-bearing and are not configurable away:

- **STT review** — a transcript is shown to you before anything acts on it. Misheard speech never silently becomes an action.
- **Side-effect preview** — sending mail, adding a reminder or calendar event, messaging someone, or writing outside the Twin always produces a preview card first. Reads never need approval.

## Layout

```
cortex/            the brain
  cortex/
    main.py              CLI entry point; loads .env, binds WSS + HTTP
    server.py            Glass-facing WebSocket endpoint and turn orchestration
    whisper_pipeline.py  whisper.cpp STT (two-tier: fast partial, accurate final)
    classifier.py        one cheap call deciding SIMPLE vs COMPLEX
    router.py            SIMPLE path — event to a single adapter dispatch
    claude_sdk_agent.py  COMPLEX path — in-process Claude Agent SDK runner
    agent_brief.py       single source of truth for the brief handed to the agent
    prompts.py           every tunable prompt constant, in one file
    llm_cache.py         the single chokepoint for LLM calls: cache, retry, JSON repair
    twin.py              Twin reader/writer and CHANGELOG appender
    face_index.py        on-device face recognition (InsightFace, CoreML)
    enroll_parser.py     spoken bio to a structured person record
    insight_engine.py    proactive surfacing
    distiller.py         turns implicit feedback into Twin updates
    sessions.py          conversation threads · session_router.py · session_browser.py
    markdown_runs.py     Markdown to styled runs for the HUD
    mail_inbound.py      inbound email to a HUD card you can dictate a reply to
    http.py              management HTTP surface · control_plane.py  state mirror
    schema.py            Pydantic wire models · ids.py · tcc_check.py · audio_buffer.py
  launchd/               plist template, installed by scripts/install.sh

tool-agent/        the hands
  adapters.yaml          which adapters load — this file is the capability surface
  tool_agent/
    server.py            WS server on loopback · registry.py  adapter loading
    adapters/            one file per adapter (see the table below)

twin-seed/         the Digital Twin, as shipped
scripts/           install + operate (start/stop/restart/status/logs)
test-harness/      end-to-end flows that drive Cortex like a real client would
```

### Adapters

`adapters.yaml` is the whole capability surface — an adapter that is not listed there
cannot be reached, no matter what a model asks for.

| Adapter | What it can do |
|---|---|
| `claude_code` | run the Claude Code CLI: draft, run, continue, list sessions |
| `applescript_mail` | read, search, draft, send via Mail.app — sending always previews first |
| `applescript_calendar` · `applescript_reminders` | read and create events and reminders |
| `apple_notes` | read and write Notes.app |
| `apple_shortcuts` | invoke any user-defined Apple Shortcut |
| `imessage` | send via AppleScript; read recent threads from `chat.db` (needs Full Disk Access) |
| `safari_state` | current tab, all tabs, recent history |
| `fs` | read, write, append, grep, list, delete — writes restricted to allowlisted roots |
| `system_status` | battery, focus mode, foreground app, network, time |
| `twin_query` | semantic Q&A over the Twin |
| `echo` | spine test |

There is deliberately **no** image-to-text adapter: a camera frame rides into the planner
as a multimodal image block and the model reads it directly, rather than being flattened
to a lossy text description first.

## The Digital Twin

Everything the system knows about you is Markdown on disk at `~/constellation/twin/`,
seeded from [`twin-seed/`](twin-seed/). No database, no embeddings store, no lock-in —
`grep` works, `vim` works, `git init` works.

```
identity.md              who you are, how you want to be written for
people/core/<slug>.md    one file per person
projects/<Name>.md       one file per project, including where its code lives
memos/<slug>.md          saved notes, often with an embedded photo
follow-ups.md            commitments you made to people
receipts/<date>.md       append-only log of every action taken on your behalf
CHANGELOG.md             append-only log of every write the system made to the Twin
.claude/skills/          prescriptive style guides the agent auto-discovers
```

Cortex respects mtime: if you edited a file by hand, your version wins the conflict.
`twin-seed/identity.md` ships as a template to fill in, and the two people in
`twin-seed/people/core/` are fictional examples — delete them.

## Setup

**Requirements**: macOS on Apple Silicon · Python 3.11+ · [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
(`brew install whisper-cpp`) · the [Claude Code CLI](https://claude.com/claude-code) ·
an OpenAI API key.

```bash
git clone https://github.com/MRziyi/Constellation-Server.git
cd Constellation-Server

cp .env.example .env       # then fill in your keys
$EDITOR .env

./scripts/install.sh       # venvs, Twin seed, launchd jobs, start
```

`install.sh --twin-only` seeds just the Twin; `--skip-launchd` installs the code without
daemonising. To let the glasses reach Cortex over the network rather than loopback, set
`CORTEX_HOST` to the address they can reach before running the installer.

Day-to-day:

```bash
./scripts/status.sh        # launchd jobs, ports, health, glasses
./scripts/restart.sh cortex
./scripts/logs.sh
./scripts/dev-local.sh     # local demo bring-up, no glasses or relay needed
```

Then fill in `~/constellation/twin/identity.md` — the system is only as good as that file.

### macOS permissions

The AppleScript adapters need TCC grants (Automation for Mail, Calendar, Reminders,
Notes, Messages, Safari; Full Disk Access for reading `chat.db`). `cortex/cortex/tcc_check.py`
reports what is missing at startup. Granting these on a headless machine is genuinely
awkward — that problem is documented in
[DEPLOYMENT-mac-mini-migration.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/DEPLOYMENT-mac-mini-migration.md).

## Configuration

Secrets go in `.env` (git-ignored). Behaviour is tuned through environment variables; see
[`.env.example`](.env.example) for the full list with defaults. The ones you are most
likely to touch:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | required — router, classifier, and vision |
| `GROQ_API_KEY` | optional — fast inference for latency-sensitive calls |
| `CORTEX_ROUTER_MODEL` · `CORTEX_CLASSIFIER_MODEL` | model choice per stage |
| `CORTEX_AGENT_MODEL` · `_FAST` · `_DEEP` · `CORTEX_AGENT_EFFORT` | agent path model tiers |
| `CORTEX_SDK_MAX_BUDGET_USD` | hard spend ceiling for one agent run |
| `CONSTELLATION_INSIGHT_ENGINE` | enable proactive surfacing |
| `CONSTELLATION_FACE_MODEL` · `_THRESHOLD` · `_DET_SIZE` | face recognition tuning |
| `WHISPER_CLI` · `WHISPER_SERVER` | paths for local STT |

## Testing

`test-harness/` drives Cortex over its real WebSocket interface — these are end-to-end
flows against live services, not unit tests. Nothing sends for real unless you ask:

```bash
./scripts/start.sh
python test-harness/full_loop.py
TEST_RECIPIENT=you@example.com python test-harness/mail_flow.py   # dry run
TEST_RECIPIENT=you@example.com python test-harness/mail_flow.py --send-for-real
```

## Known limitations

- **Single-user by construction.** The author's name is in the system prompts and Twin paths; multi-tenancy is a real refactor.
- **macOS-only**, and dependent on AppleScript automation surfaces that Apple can change.
- **Not privacy-hardened.** Task content reaches cloud models. The Twin stays on disk and face recognition runs on-device, but this is not a private-by-design system.
- **No test suite** in the unit sense — correctness is checked by end-to-end flows and daily use.

## Related

[Constellation](https://github.com/MRziyi/Constellation) (design and architecture) ·
[Constellation-Glass](https://github.com/MRziyi/Constellation-Glasses) (the eyewear client)

## License

[Apache License 2.0](LICENSE).
