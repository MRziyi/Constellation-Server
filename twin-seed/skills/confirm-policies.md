---
type: skill
name: confirm-policies
description: Per-tool preview/auto policy. Decides whether each tool action shows a preview HUD card or executes silently.
created: 2026-05-24T00:00:00Z
updated: 2026-05-24T00:00:00Z
share: [claude, gpt]
confidence: 1.0
---

# Confirm Policies (v1 defaults)

Decides whether each tool action triggers a `preview_action` HUD card (user must SEND/FEEDBACK) or
auto-executes silently. The default for unknown tool/action combinations is
`preview-default` — Cortex always errs on showing a preview.

Format: each row is `{tool}:{action} : {policy}`. Policies:

- **preview-always** — Always show preview. Even auto-confirm policies on other rules can't skip this.
- **preview-default** — Show preview by default. Confirm-policies-from-skills can override down.
- **auto** — Skip preview; execute immediately. Receipt is still written.
- **deny** — Refuse to dispatch this combination. Used to lock dangerous combos.

```yaml
# === ALWAYS preview, no override possible ===
applescript_mail:send         : preview-always
fs:delete                     : preview-always
fs:write                      : preview-always
claude_code:run               : preview-always
claude_code:kill              : preview-always
applescript_reminders:delete  : preview-always
applescript_calendar:add_event: preview-always

# === Auto-execute (low risk, frequent enough that preview is annoying) ===
applescript_reminders:add     : auto
applescript_mail:read_current : auto
applescript_mail:list_inbox   : auto
applescript_calendar:list_today : auto
applescript_calendar:list_range : auto
applescript_calendar:find_conflict : auto
applescript_calendar:get_event  : auto
applescript_reminders:list    : auto
applescript_reminders:complete: auto
fs:read                       : auto
fs:grep                       : auto
fs:list                       : auto
fs:append                     : auto   # append-only; safer than write
claude_code:draft             : auto   # draft has no side-effects by design
claude_code:get_status        : auto
local_face_recognition:detect : auto
local_face_recognition:match  : auto
local_face_recognition:embed  : auto

# === Default for anything not listed ===
"*"                           : preview-default
```

## Special cases

### `claude_code:send_keys`

The `send_keys` action's effect depends entirely on **what keys**:
- `y\n` (permission grant) — already implicitly confirmed by the user choosing ONCE/SESSION on
  the reverse-wake card, so `auto` here is fine.
- arbitrary keys — could be dangerous. `preview-always`.

Adapter implementation must distinguish: if `args.keys` is one of `["y\n", "n\n", "s\n"]`,
treat as `auto`; otherwise `preview-always`. This is a per-args policy, not a per-action one;
it lives in the adapter, not this file.

### `fs:write` to Twin

`fs:write` is `preview-always` here, BUT Cortex's own writes to `~/constellation/twin/` go
through Cortex's Twin Writer module (NOT through Tool Agent's fs adapter), which has its own
confidence-gated protocol per `skills/twin-write-policy.md`. So this rule applies to non-Twin
writes (the rare cases where Cortex needs to write a non-Twin file via fs).

## How Cortex applies this

1. Cortex composes a dispatch plan with N subtasks.
2. For each subtask, Cortex looks up `{tool}:{action}` in this file.
3. If `preview-always` → set `requires_confirm = true` regardless of plan.
4. If `auto` → set `requires_confirm = false`.
5. If `preview-default` → set `requires_confirm = true` UNLESS Cortex Router explicitly set it
   to `false` in the plan (rare; for known-safe special cases).
6. If `deny` → reject the subtask; either replace it with a safer alternative or abort the plan
   with a HUD error.

## You can edit this

If something feels too noisy (Cortex keeps previewing something you trust), flip it to `auto`.
If something feels too quiet (Cortex auto-executed something you wanted to review), flip it to
`preview-always` or `preview-default`.

Default is conservative: show preview unless explicitly downgraded.

---

*See [DESIGN.md §2 P3 Preview Before, Receipt After](../../DESIGN.md) for the principle this file enforces.*
