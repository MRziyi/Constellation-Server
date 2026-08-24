---
type: schema
created: 2026-05-24T00:00:00Z
updated: 2026-05-24T00:00:00Z
share: none
---

# Twin Meta-Schema

This is the **meta-schema** — Cortex reads this file before writing to the Twin to remember how
every entity type is structured. It mirrors [DATA-MODEL.md](https://github.com/MRziyi/Constellation/blob/main/docs/server/DATA-MODEL.md) §4–§8 but lives
*inside* the Twin so Cortex always has the schema in arm's reach.

## Universal frontmatter (every file)

```yaml
---
type: <one of: identity | skill | person | encounters | project | receipt
       | commitment | interest | conversation | memory | schema>
created: <ISO 8601 UTC>
updated: <ISO 8601 UTC>
share: <"none" | "all" | [list of labels e.g. "claude", "gpt"]>
confidence: <0.0 - 1.0, omit if type=receipt (always 1.0)>
sources: <list of evt_*, rcpt_*, pulse_* ids>
---
```

The body below the frontmatter is free-form markdown. Sections, bullet points, [[wikilink]]s
to other files all OK.

## Per-type frontmatter additions

### `identity` (just one file, `identity.md`)

No type-specific fields. The body has standard sections: Basic / Currently doing / Operating
philosophy / Voice & taste / How I think / Network / Long-term interests / Health / AI usage stack.

### `skill`

```yaml
name: <kebab-case, same as filename minus .md>
description: <one-line — used by LLM to decide whether to load this skill>
```

A skill is a **prescriptive document** ("how AI should act as me when doing X"). Density varies
from a 3-bullet preference to a multi-file SKILL.md with procedures and self-check (see the
example at `transcript-to-insights/SKILL.md` in Zack's Chrono project for the high-density end).
Don't force a uniform template — match density to content.

### `person` (in `people/core/{slug}.md`)

```yaml
relation: <friend | colleague | family | mentor | mentee | acquaintance | ...>
affiliation: <org or null>
fields: <list of topics; e.g., [HCI, AR, education]>
last_seen: <ISO date>
last_contact: <ISO date>
preferred_contact: <"email" | "imessage" | "wechat" | ...>
aliases: <list of how this person is also referenced — first name, nickname, etc.>
```

### `encounters` (single file, `people/encounters.md`)

No type-specific fields. Body is a series of `## Person Name (yyyy-mm-dd)` sections; each section
captures what's known so far. Promote to `people/core/{slug}.md` when ≥ 3 appearances OR linked
to a commitment OR user explicitly says "promote".

### `project`

```yaml
status: <"active" | "paused" | "shipped" | "archived">
repo_path: <local path if applicable>
collaborators: <list of person slugs>
```

### `commitment` (P6 scans these)

```yaml
due: <ISO date or relative string like "next week">
to: <person slug or null>
status: <"open" | "done" | "abandoned">
priority: <"high" | "medium" | "low">
source_conversation: <path to conversation file>
```

### `interest` (P6 scans these)

```yaml
topic: <human-readable name>
signal_strength: <0.0 - 1.0, decays over time, increases on signal>
last_signal_at: <ISO date>
aliases: <list of how this topic is also referenced>
```

### `receipt` (in `receipts/{yyyy-mm-dd}.md`, one file per day)

```yaml
date: <yyyy-mm-dd>
```

The body has `## HH:MM:SS — {action} [rcpt_id]` sections per receipt. See
[DATA-MODEL §8.1](https://github.com/MRziyi/Constellation/blob/main/docs/server/DATA-MODEL.md) for full example.

### `conversation` (in `conversations/{yyyy-mm-dd}/{HH-MM}-{slug}.md`)

```yaml
date: <yyyy-mm-dd>
participants: <list of person slugs (+ "you")>
topic: <one-line>
duration_minutes: <int>
location: <free text>
```

### `memory` (under `memories/`)

Free schema; depends on subdirectory. Initial schema:
- `memories/faces/{person-slug}/metadata.md`: standard frontmatter, plus `person` field.
  The directory also contains face crops + `embeddings.json` (managed by Tool Agent's
  `local_face_recognition` adapter).

### `schema` (just this file and any others under `_system/`)

For Cortex's reference; not user-facing.

## Write protocol

Cortex MUST:
1. Read the target path's mtime before generating a new version.
2. If the path doesn't exist → create + write CHANGELOG entry + update `_system/TOC.md`.
3. If the path exists AND no mtime conflict AND confidence ≥ 0.7 (per
   `skills/twin-write-policy.md`) → overwrite + write CHANGELOG entry (with diff summary) +
   update `_system/TOC.md`.
4. Otherwise → write the proposed diff to `_system/pending/{date}-{slug}.diff.md` and surface
   "N Twin reviews pending" on the next morning's HUD peek.

## CHANGELOG format

```markdown
### {HH:MM} — {short label} [src:{evt_id|pulse_id|cron}]
- Added/Updated/Removed: {path}
  {- optional bullet list of field-level changes}
```

## TOC.md format

```markdown
# Twin TOC (auto-maintained)

## {section}
| path | description | updated |
|---|---|---|
| ... | ... | yyyy-mm-dd |
```

Each section corresponds to a Twin subdirectory. TOC.md is rebuilt on every successful Twin
write (incremental update; daily full rebuild for verification).

## Don'ts

- **Don't write to** `receipts/{past-date}.md` after the day has rolled over. Receipts are
  append-during-day, then frozen.
- **Don't modify** `_system/schema.md` (this file) unless you're explicitly evolving the schema
  itself; doing so should be a deliberate decision logged with reasoning in CHANGELOG.
- **Don't** write outside the Twin root (`~/constellation/twin/`).

---

*This file is the floor. Everything else respects this contract.*
