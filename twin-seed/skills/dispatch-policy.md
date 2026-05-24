---
type: skill
name: dispatch-policy
description: Learned tool routing preferences. Cortex appends a hint here every time user override changes the dispatch.
created: 2026-05-24T00:00:00Z
updated: 2026-05-24T00:00:00Z
share: none
confidence: 1.0
---

# Dispatch Policy Hints

Cortex's learned preferences for which tool to dispatch to for a given intent. Cortex appends
to this file when a user override demonstrates a stable preference. Cortex reads this file as
part of the Router context_pack on every dispatch.

Format: one bullet per hint, with date + intent class + chosen tool + (optional) reason.

## Hints

*(Empty — Cortex will append as it observes your overrides.)*

## Example shape (for Cortex to follow when appending)

```markdown
- 2026-05-25: For `email_reply` intents addressed to a `people/core/` person, prefer
  `claude_code:draft` over inline draft. Reason: user accepted CC's tone 5 times after
  rejecting inline.
- 2026-05-26: For `code_refactor` intents in `R08-dev/`, dispatch through
  `claude_code` (not `applescript_*`). Reason: user explicitly said "send to Claude Code".
- 2026-06-02: De-prefer `applescript_reminders` for intents containing "tomorrow morning"
  — user always asks to use Calendar instead.
```

## How Cortex uses this

When composing a dispatch plan, Cortex:
1. Reads this file as part of context_pack.
2. Includes the hints in the Router GPT prompt.
3. Router weighs hints when choosing tools (treats them as soft preferences, not hard rules).

## When to prune

If a hint contradicts another, the **newer** one wins (Cortex sorts by date). You can
hand-prune this file anytime — Cortex will respect mtime. If you delete a hint that was
auto-added, expect Cortex to potentially re-learn it; if so, the next learning candidate
shows up in `_system/pending/skill-updates/` for your review before auto-appending.

---

*Don't put preferences here manually — those go in your other `skills/*.md`. This file is
for **learned tool routing** specifically.*
