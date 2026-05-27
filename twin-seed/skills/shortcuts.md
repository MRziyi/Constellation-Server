---
managed_by: constellation-glass-app
schema_version: 1
---

# Shortcuts

**One-tap fire-and-forget prompts.** Each shortcut binds a preset text
prompt (optionally bundled with a fresh camera frame) to a physical
trigger — either a Halo Ring gesture (per
[`halo-ring-plugin-protocol.md`](../../docs/cross-device/halo-ring-plugin-protocol.md))
or a long-press of the temple button when the in-app picker is open.

The whole point is to **skip the voice step**: when you already know what
you want to ask ("what's in front of me?", "log this whiteboard"), tap
once and the prompt + image fire to Cortex automatically. The HUD then
shows the same preview-action / info-card flow as a voice invocation.

If a shortcut feels like it should also "let me say something" — that's
not a shortcut anymore, that's a normal voice invoke with an opening
prompt. Shortcuts are explicitly the no-voice path.

**Edit through the in-app UI** (`Settings → Shortcuts → New Shortcut` or
tap an existing row). The schema is parsed by `cortex.shortcuts_store` —
hand-editing this file works but the app will rewrite on the next save,
preserving only fields the schema knows about.

## Schema (one block per shortcut)

```markdown
<!-- shortcut:start -->
id: kebab-case-id          # stable identifier; used by HaloActionsProvider as `shortcut_{id}`
name: Human-readable name  # appears as the row title in Settings + Halo Ring picker
photo: true                # capture a fresh camera frame and bundle with the prompt? (default false)
created: 2026-05-26        # ISO date, written by the app on first save
updated: 2026-05-26        # ISO date, last edit
<!-- shortcut:body -->

<the preset prompt body, free-form markdown — this is the literal text
sent to Cortex as the user message>

<!-- shortcut:end -->
```

**Why the delimited HTML-comment markers** instead of YAML frontmatter per
block: standard frontmatter only works at the start of a file. We need
many blocks per file. Comment markers are invisible in rendered markdown,
easy to grep, and trivially parseable. The choice mirrors how the
[twin-write-policy.md](twin-write-policy.md) and other multi-block files
handle structured content.

## Sample shortcuts

<!-- shortcut:start -->
id: whats-in-front
name: What's in front of me?
photo: true
created: 2026-05-26
updated: 2026-05-26
<!-- shortcut:body -->

Describe what's in the attached photo. One-sentence summary first, then
two more sentences with any details that look interesting or unusual.
If you recognize a person, name them only if they're in `people/core/`.
<!-- shortcut:end -->

<!-- shortcut:start -->
id: quick-capture-person
name: Quick capture person
photo: true
created: 2026-05-26
updated: 2026-05-26
<!-- shortcut:body -->

Identify this person from the attached photo. If they match an existing
file under `people/core/`, surface the archive (most recent meeting notes
+ commitments). If unknown, propose adding them to `people/encounters.md`
with what you can infer from the photo (clothing, setting, time of day)
and any prior context from this device session.
<!-- shortcut:end -->

<!-- shortcut:start -->
id: ocr-save-to-today
name: OCR & save to today
photo: true
created: 2026-05-26
updated: 2026-05-26
<!-- shortcut:body -->

Run OCR on the attached photo. Save the extracted text to today's
`receipts/<YYYY-MM-DD>.md` under a `## OCR captures` heading with the
timestamp. If the text looks structured (whiteboard list, receipt,
business card), surface a brief structured preview as a card; else just
confirm the save.
<!-- shortcut:end -->
