---
type: skill
name: pulse-feedback
description: What P6 Insight Engine has learned from user's dismiss/engage history. Cortex appends after each pulse interaction.
created: 2026-05-24T00:00:00Z
updated: 2026-05-24T00:00:00Z
share: none
confidence: 1.0
---

# Pulse Feedback (P6 Insight Engine learning)

Cortex's P6 Insight Engine writes here after each pulse the user dismisses or engages with. Over
time, this file becomes a profile of what kind of pulses you actually want vs. find annoying.

Format: one bullet per learned rule, with date + pattern + effect on future pulses.

## Learned rules

*(Empty — Cortex appends as you interact with pulses.)*

## Example shape

```markdown
- 2026-06-01: Pulses about "RL papers from Sutton" → dismissed 4 times during work hours (9–17h).
  Effect: lower priority during work hours; OK to push evenings/weekends.
- 2026-06-08: Pulses about "Jane's commitment review" → engaged within 5 min, twice.
  Effect: maintain priority; tag as high-engagement category.
- 2026-06-12: Cross-domain insights (e.g., "person X mentioned topic Y from interests/") →
  consistently engaged when same-day; ignored when older than 1 week.
  Effect: tighten freshness window to 7 days for this category.
```

## How Insight Engine uses this

When Insight Engine evaluates a candidate pulse:
1. Reads this file.
2. Includes the learned rules in the LLM evaluation prompt:
   "Given these learned preferences, is this pulse worth surfacing now?"
3. If a rule strongly weights against surfacing → drop.
4. If a rule strongly weights for → push higher in priority.

The LLM evaluator never auto-applies a rule rigidly; it weighs the rule against the current
situation. The user can hand-edit / prune anytime.

## Default behaviour (seed)

With this file empty:
- All pulse candidates that pass LLM "is this surprising/interesting" check → push.
- No time-of-day filtering.
- No category-specific tuning.

You'll see this file populate over your first 1–2 weeks of use as Insight Engine learns from
your reactions.

---

*See [DESIGN.md §2 P6 Surprising Insight](https://github.com/MRziyi/Constellation/blob/main/docs/constitution/DESIGN.md) for the principle this file refines.*
