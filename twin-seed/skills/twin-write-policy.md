---
type: skill
name: twin-write-policy
description: Confidence threshold + conflict rules for Cortex writing to the Twin. Edit to tune how aggressively Cortex modifies your archive.
created: 2026-05-24T00:00:00Z
updated: 2026-05-24T00:00:00Z
share: [claude, gpt]
confidence: 1.0
---

# Twin Write Policy

Controls Cortex's behaviour when modifying the Twin.

## Confidence threshold

```yaml
mutating_confidence_threshold: 0.7
```

Cortex computes a confidence score (0.0–1.0) for every proposed mutation. The score reflects
the LLM's certainty + the strength of the source signal (e.g., direct user statement → high;
inferred from oblique mention → lower).

- **Confidence ≥ threshold** AND no mtime conflict → write immediately + append CHANGELOG.
- **Confidence < threshold** OR mtime conflict → write to `_system/pending/skill-updates/`
  (for skill mods) or `_system/pending/entity-updates/` (for person/project mods) for user
  review.

Lower the threshold (e.g., 0.5) → Cortex is more aggressive, more pending reviews.
Raise the threshold (e.g., 0.9) → Cortex is conservative, fewer auto-writes.

## Mtime conflict

```yaml
mtime_check: enabled
mtime_grace_seconds: 5
```

If a file's mtime is newer than Cortex's last-known-read time (with a 5 s grace for clock
skew), Cortex treats this as "user edited this file since I last looked" and skips automatic
overwrite. The proposed mutation goes to pending/ for user review.

## Additive vs mutating

```yaml
additive_writes: auto      # creating new files: always immediate
mutating_writes: threshold # modifying existing files: subject to confidence threshold
appending_writes: auto     # appending to existing files (CHANGELOG, dispatch.md, etc.): always immediate
```

Appending is treated as safe (doesn't destroy existing content) and goes immediate.

## CHANGELOG entry policy

```yaml
changelog_for_every_write: true
changelog_field_level_diff: true
```

Every write — additive, mutating, appending — produces a CHANGELOG entry. Field-level diffs
(showing exactly which YAML frontmatter fields or markdown sections changed) are included for
mutating writes.

## TOC.md update policy

```yaml
toc_incremental_update: true
toc_daily_full_rebuild: true
toc_full_rebuild_hour: 3   # 3 AM local
```

Every Twin write triggers an incremental TOC.md update for the modified path. A daily 3 AM
full rebuild catches any drift.

## Pending review surfacing

```yaml
pending_review_surface: morning_hud_peek
pending_review_max_age_days: 14
```

When `_system/pending/` has entries, Cortex surfaces them via the morning HUD peek (e.g.,
"3 Twin reviews pending"). Pending items older than 14 days are auto-archived (moved to
`_system/pending/archive/`) rather than nagging forever.

## Snapshot / undo

```yaml
snapshots: disabled   # v1; v1.5+ may enable
```

v1 does NOT snapshot files before mutation. If Cortex mutates a file you didn't want changed,
you must:
1. Read CHANGELOG to see what changed.
2. Hand-restore by `vim`.

If this proves painful, v1.5 may add `_system/snapshots/{date}/{path}` to keep N days of
pre-mutation snapshots. Per [DATA-MODEL §12](../../DATA-MODEL.md), this is a "need-driven"
feature, not v1.

## You can edit this

These are tuneable defaults. If you want a more or less aggressive Cortex:

- Want Cortex to ask more often? Raise `mutating_confidence_threshold` to 0.8 or 0.9.
- Want Cortex to act more autonomously? Lower to 0.5 (with the trade-off of more wrong writes).
- Want to disable mtime check (always trust Cortex)? Set `mtime_check: disabled` (NOT recommended).

---

*Seed value of 0.7 chosen as a balance: aggressive enough that you don't drown in pending reviews; conservative enough that low-quality LLM inferences don't pollute your Twin.*
