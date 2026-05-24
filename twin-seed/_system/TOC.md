# Twin TOC (auto-maintained by Cortex)

This file is Cortex's index of the entire Twin — a `path | description | updated` table per
section. Cortex updates this incrementally on every Twin write; a daily full rebuild verifies
nothing drifted.

`description` is what an LLM sees when deciding "should I load this file?" — the
Anthropic-Skill-style hook. Keep them short (one line) and informative.

---

## identity

| path | description | updated |
|---|---|---|
| identity.md | Zack's core archive: basic info, operating philosophy, voice & taste, AI stack | 2026-05-24 |

## skills/

| path | description | updated |
|---|---|---|
| skills/email-style.md | How Zack writes emails (tone, greetings, sign-offs, don'ts) | 2026-05-24 |
| skills/code-style.md | How Zack writes code review comments and PR descriptions | 2026-05-24 |
| skills/dispatch-policy.md | Learned tool routing preferences; Cortex appends on user overrides | 2026-05-24 |
| skills/confirm-policies.md | Per-tool preview/auto policy (when to preview, when to auto-execute) | 2026-05-24 |
| skills/pulse-feedback.md | What P6 Insight Engine has learned from user dismiss/engage history | 2026-05-24 |
| skills/twin-write-policy.md | Confidence threshold + conflict rules for Cortex writing to Twin | 2026-05-24 |
| skills/insight-engine.md | P6 scan frequency, sources, evaluation prompt | 2026-05-24 |
| skills/claude-code-control.md | Regex patterns + keys for tmux-based Claude Code control | 2026-05-24 |

## people/

*(Initially empty. Cortex populates `people/core/*.md` and `people/encounters.md` as you
encounter people through Voice Invoke / Quick Shortcut / face recognition.)*

## projects/

*(Initially empty. Hand-seed or let Cortex extract from your conversations.)*

## commitments/

*(Initially empty. Cortex extracts commitments from voice intents containing promises like
"I'll do X by Y"; surfaces them for P6 follow-up.)*

## interests/

*(Initially empty. Cortex builds these from recurring topics in conversations.)*

## conversations/

*(Initially empty. Cortex creates per-date subdirectories when you enable transcription.)*

## receipts/

*(Initially empty. Cortex creates `receipts/{today}.md` on first dispatched action.)*

## memories/

*(Initially empty. Tool Agent's `local_face_recognition` writes here when faces are added.)*

---

*Last full rebuild: 2026-05-24T00:00:00Z (seed).*
