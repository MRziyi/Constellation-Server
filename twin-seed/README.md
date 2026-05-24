# Constellation Twin (seed)

This is the **seed copy** of your Digital Twin. On first run, copy this entire directory to
`~/constellation/twin/`:

```bash
mkdir -p ~/constellation
cp -r ~/Code/Projects/Constellation/twin-seed ~/constellation/twin
```

After the copy, Cortex Agent will take over: it will read, append, and edit files here as you
use Constellation. You can `vim` anything anytime — Cortex respects mtime conflicts and won't
silently overwrite your edits.

## What lives here

| Path | Purpose | Author |
|---|---|---|
| [`identity.md`](identity.md) | Your core archive — name, role, voice, taste | You (seed) + Cortex (implicit learning) |
| [`CHANGELOG.md`](CHANGELOG.md) | Append-only log of every Cortex modification to this Twin | Cortex |
| [`skills/`](skills/) | Prescriptive how-to docs ("how AI should act as me") | You (seed) + Cortex (implicit learning) |
| [`people/`](people/) | Relationship archives (core/ + encounters.md) | Cortex (mostly), you can edit |
| [`projects/`](projects/) | Active project docs | You + Cortex |
| [`commitments/`](commitments/) | Things you've said you'd do (P6 scans) | Cortex (from conversation extraction) |
| [`interests/`](interests/) | Long-running topics (P6 scans) | Cortex |
| [`conversations/{date}/`](conversations/) | Meeting / conversation transcripts | Cortex |
| [`receipts/{date}.md`](receipts/) | Daily action log | Cortex |
| [`memories/faces/`](memories/) | Face recognition embeddings | Tool Agent (`local_face_recognition`) |
| [`_system/`](_system/) | Cortex's meta-tools: schema, TOC, pending review queue | Cortex |

## How to use

**You don't actively maintain this.** Constellation does — via the Implicit Learning Loop
(see [DATA-MODEL.md §9](../DATA-MODEL.md)). When you give feedback during a task, when you
override a Cortex suggestion, when you repeat a pattern — Cortex extracts a learning
candidate and (eventually, after your approval where confidence is low) writes here.

But you **can** `vim` anything anytime. Specifically expect to hand-edit:
- [`identity.md`](identity.md) — fill in the "Voice & taste" + "Operating philosophy" sections in your own words
- [`skills/email-style.md`](skills/email-style.md) — refine as you find Cortex's first drafts don't match
- [`skills/dispatch-policy.md`](skills/dispatch-policy.md) — initially empty; Cortex appends; you can prune

## What's NOT here yet

Seed copy ships with:
- ✓ Identity + 8 default skill docs + `_system/` scaffolding
- ✗ No people, projects, commitments, interests, conversations, receipts, memories (those build up via use)

The first time you use Voice Invoke, Cortex starts populating these from your interactions.

## Reference

See [DATA-MODEL.md](../DATA-MODEL.md) for full schema, write protocol, and the Implicit Learning Loop.

---

*Welcome to your Twin. Where your senses go, your mind follows.*
