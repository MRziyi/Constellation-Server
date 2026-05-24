---
type: skill
name: reminder-style
description: How Zack wants reminder titles phrased (short, imperative, no articles)
created: 2026-05-24
updated: 2026-05-24
share: [claude, gpt]
---

# Reminder Style

When composing the `title` for `applescript_reminders.add`, follow these:

## Rules

- **Action verb first**, imperative mood: "call X", "email Y", "review Z"
- **No articles** (a / the): "review draft", not "review the draft"
- **Max 5 words** — if intent is longer, push detail into `notes`, not `title`
- **No emoji**, no trailing punctuation, no `!` or `…`
- For people, **first name only** unless ambiguous: "Mike", not "Mike Chen"

## Examples

| User said | ✓ title | ✗ title |
|---|---|---|
| "remind me to grab coffee with Mike next Tuesday at 3pm" | `coffee with Mike` | `Grab coffee with Mike next Tuesday!` |
| "remind me to send Jane the revised draft tomorrow" | `email Jane revised draft` | `Send Jane the revised draft tomorrow` |
| "remind me to pick up dry cleaning Friday" | `pick up dry cleaning` | `Pick up the dry cleaning on Friday` |

## Why

Reminders sidebar is narrow on every Apple surface (phone, watch, Mac mini menu bar).
Short imperative titles scan faster. Long sentences get truncated and lose the verb,
which is the part you actually need to remember.
