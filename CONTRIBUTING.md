# Contributing

Constellation is a personal research prototype that happens to be public. It is
developed by one person for their own daily use, so please calibrate
expectations accordingly:

- **Issues are welcome**, especially "this document is wrong / contradicts that
  one" and "this design decision has a failure mode you didn't consider."
  Reports like these are the main reason the repository is public.
- **Pull requests** are accepted but reviewed slowly, and features that only make
  sense for a different deployment will usually be declined rather than merged.
  Open an issue before writing anything substantial.
- **Do not expect it to run for you.** See "Known limitations" in the README.

## The one rule that matters

[`SOURCE-OF-TRUTH.md`](https://github.com/MRziyi/Constellation/blob/main/docs/constitution/SOURCE-OF-TRUTH.md)
in the design repository
records the project's original intent verbatim. Design documents may extend and
specify it; they may not silently contradict it. If a change requires the intent
itself to move, that change must add a dated entry to the revision log in that
file saying what changed and what it supersedes.

## Documentation conventions

- The constitution and some glass-side notes are written in Chinese or mix
  Chinese and English. That is intentional; don't mass-translate them.
- User-facing strings in the glass app are **English only**.
- Design documents live in the [Constellation](https://github.com/MRziyi/Constellation) repository, not here. Code comments should link to them rather than restate them.
- Design documents state trade-offs explicitly: what was chosen *and* what was
  given up. A document that only lists benefits is incomplete.

## Before opening a PR

- Don't commit secrets. `.env` files, keystores, and signing material are
  `.gitignore`d in every repository — keep it that way. New configuration goes in
  `.env.example` with a safe default, never a real value.
- Don't commit machine-specific values: absolute home paths, Tailscale
  addresses, device serial numbers, or personal email addresses.
- Vendor SDK documentation is not redistributed here. Link to the source
  instead of pasting it in.
