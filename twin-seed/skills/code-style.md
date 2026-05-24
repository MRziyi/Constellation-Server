---
type: skill
name: code-style
description: How Zack writes code review comments and PR descriptions. Direct + explain why + suggest don't demand.
created: 2026-05-24T00:00:00Z
updated: 2026-05-24T00:00:00Z
share: [claude]
confidence: 1.0
---

# Code Style (Review + PR Descriptions)

## PR review comment tone

Direct + always include **why**, not just **what**.

### Rules

1. **Always state the reason.**
   - ✓ "this is a race — `cache.put` isn't atomic with the read above"
   - ✗ "should fix this"

2. **Critique the code, not the author.**
   - ✓ "this abstraction is too wide for the call sites it has"
   - ✗ "you wrote this wrong"

3. **Suggest, don't demand.** Use `Consider...` / `What about...` / `Could we...`.
   - ✓ "Consider returning a `Result` here — the caller has to handle the failure mode anyway"
   - ✗ "Use Result"

4. **Big changes → single comment thread.** Small nits → inline.

5. **Show alternatives.** When suggesting a change, sketch what it'd look like, even briefly.

6. **No nit-spamming.** If there are >5 nits, suggest a pass on linter config instead of leaving them all inline.

### Examples

✓ Good:
> This duplicates the validation in `validate_user_input` (line 23). Consider extracting a
> shared helper — both call sites are doing exactly the same thing, and the duplication will
> drift.

✗ Bad:
> duplicate

## PR description format

A PR description should be readable by someone who **didn't** write the code.

### Template

```
## Summary
{1-3 bullet points — what changed, in plain English}

## Why
{1-2 sentences — what problem does this solve}

## Test plan
- [ ] {a check}
- [ ] {another check}
```

### Don'ts

- ✗ Don't list every file touched. The diff already shows that.
- ✗ Don't say "various refactors" or "small fixes". Be specific.
- ✗ Don't pad with "Hopefully this helps!" — get to the point.

## Code comment tone (inside source)

- Default to **no comment**. Well-named code is self-documenting.
- Comment only when explaining **why** (a non-obvious constraint, a hidden invariant, a
  workaround).
- ✗ Never `// add 1 to i` style what-the-code-does comments.
- Module / function-level docstrings: short, one paragraph max.

---

*Update this if Cortex writes code that "isn't you".*
