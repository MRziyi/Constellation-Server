---
type: skill
name: insight-engine
description: P6 Insight Engine config — scan frequency, sources, evaluation prompt. Cortex reads this on startup and per-scan.
created: 2026-05-24T00:00:00Z
updated: 2026-05-24T00:00:00Z
share: [claude, gpt]
confidence: 1.0
---

# Insight Engine (P6 Surprising Insight)

Configuration for Cortex's P6 module: how often it scans, where it looks, what counts as
"surprising enough to push".

## Scan frequency

```yaml
twin_watcher_interval_seconds: 300   # 5 minutes
mac_event_subscribe: realtime
cron_tick_seconds: 300
```

Twin watcher and the cron tick are aligned at 5 min. Mac event subscriptions (Calendar
notifications, mail incoming, etc.) are real-time push.

## Scan sources

```yaml
sources:
  - twin/commitments/      # check for approaching due dates
  - twin/interests/        # check signal_strength + freshness
  - apple_calendar         # upcoming events
  - apple_mail             # recently received
  - mac_event_subscribers  # build watchers, GitHub webhooks (if any)
```

## What to surface

The Insight Engine looks for candidates of these types:

1. **Commitment due** — `commitments/X.md` with `due` ≤ 24 h from now and `status: open`.
2. **Cross-domain match** — a Mac event mentions a topic in `interests/` OR a person matches
   `people/core/`. ("Sergey just emailed; he's tagged interests/rl as a key person")
3. **Reflection prompt** — observed pattern in `receipts/` worth noting. ("You've sent 4
   emails to Jane this week with very different tones; want to add a note to her archive?")
4. **External signal in interest area** — RSS / GitHub PR / Twitter mention (when those
   subscribers exist) matching an interest topic.

## What NOT to surface

```yaml
exclude:
  - routine_reminders    # Apple Reminders.app already pushes these
  - calendar_events      # Apple Calendar already pushes these
  - already_pushed_24h   # don't repeat the same pulse within 24 h
  - low_confidence       # see threshold below
```

## LLM evaluation prompt

When the engine has a candidate, it runs this prompt against the Router LLM (GPT-mini):

```
Given the candidate insight below + the user's `pulse-feedback.md` history, judge:
1. Is this surprising? (something the user would NOT already see in Calendar/Reminders/Mail)
2. Is this interesting? (would the user say "huh, good catch" rather than "shut up")
3. Is the timing right? (work hours vs evening; learned rules from pulse-feedback.md)

Output: {push: bool, priority: low|med|high, reason: string}
```

The threshold for `push: true` is implicit in the LLM — but Cortex multiplicatively weighs:
- Match against learned rules in `pulse-feedback.md`
- Time-of-day appropriateness (per pulse-feedback)
- Recent dismissal density (if user dismissed 3 in a row, raise the bar for the next one)

```yaml
push_min_priority: low    # push everything LLM marks as push:true; lower bar in v1
```

## Daily summary digest

```yaml
daily_digest:
  enabled: false   # v1: off
  time: 22:00
  surfaces: morning_hud_peek
```

A future addition: instead of pushing 1-by-1, batch FYI-tier pulses into an evening digest.
Not v1 (per scope discipline).

## Receipt of pulse interactions

Every pulse Cortex pushes is logged in `receipts/{date}.md` with:
- `pulse_id`
- candidate type
- LLM evaluation reasoning
- user_decision (engage/dismiss/timeout)
- subsequent learning (if any) appended to `pulse-feedback.md`

## Cost ceiling

```yaml
gpt_eval_max_per_day: 200
```

200 LLM evaluations / day caps cost at ≈$0.50/day with gpt-4o-mini pricing. Adjust if needed.

## You can edit this

- Want pulses more aggressive? Lower `push_min_priority` to anything; lower
  `twin_watcher_interval_seconds`; raise `gpt_eval_max_per_day`.
- Want pulses chill? Disable `mac_event_subscribers`; raise interval to 30 min;
  enable `daily_digest` and direct most things there.
- Want a specific topic to never surface? Add it to `exclude` (e.g.,
  `- topic:rl_papers`) — Insight Engine drops candidates matching.

---

*Per the user's principle ([identity.md operating philosophy](../identity.md)): Cortex
surfaces what's worth noticing, not what's already in Calendar / Reminders.*
