---
type: skill
name: email-style
description: How Zack writes emails. Tone is casual + brief. Skip pleasantries; "Hey" + "— Zack"; no emoji in text.
created: 2026-05-24T00:00:00Z
updated: 2026-05-24T00:00:00Z
share: [claude, gpt]
confidence: 1.0
---

# Email Style

## Default tone
Casual but professional. **Brief**.

## Greetings
- `Hey {name}` — known peers, colleagues, anyone you've emailed before
- `Hi {name}` — formal or first email to someone
- Never: `Dear`, `To whom it may concern`, `Greetings`

## Sign-offs
- `— Zack` (default for almost everything)
- `Best, Zack` (when extra formal is appropriate — funding agencies, etc.)
- `Cheers, Zack` (close friends only, ZH-ok)

## Length
- Default: **2 – 4 sentences**.
- Long only when the content genuinely needs it — a real explanation, a real proposal.
- If you find yourself padding to make it "polite", stop and trim.

## Don'ts

- ✗ `I hope this email finds you well.`
- ✗ `Just wanted to check in...`
- ✗ `Please don't hesitate to reach out.`
- ✗ Closing pleasantries (`Looking forward to hearing back!`) — they pad.
- ✗ Emoji in the text. (口语 OK. 文字不要.)
- ✗ Excessive `!`. One per email max, used for genuine emphasis.

## Language

- English emails to English-speaking recipients.
- Chinese emails (中文邮件) for Chinese-speaking recipients: same rules, same brevity.
  - Greeting: `{Name}，` (with comma), or `Hi {Name},` if it's a 中英混 context.
  - No `您好，希望邮件已到达。` type opener.
  - Sign with `Zack` or `紫意` depending on register.

## Examples

✓ Good:
> Hey Jane —
>
> Yes, see you at 3.
>
> — Zack

✗ Bad:
> Dear Jane,
>
> I hope this email finds you well! I'm writing to confirm our meeting at 3pm today, which I'm
> very much looking forward to. Please let me know if anything changes.
>
> Best regards,
> Zachary Zhang

(Both convey "I'll be there at 3", but only one sounds like me.)

## When delegating (AI as Zack)

When Cortex / Claude Code drafts an email on my behalf:
- Read this file in full BEFORE composing.
- Use the recipient's archive (`[[people/core/{slug}.md]]`) for their preferred name + any
  notes on their communication style.
- Default to ENGLISH unless the recipient's archive says otherwise.
- Output the email body only — no commentary, no `[Subject: ...]` prefix unless asked.

---

*Update this file if you catch Cortex drafting in a style that's not you.*
