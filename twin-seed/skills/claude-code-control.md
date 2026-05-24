---
type: skill
name: claude-code-control
description: Regex patterns + response keys for tmux-based Claude Code control. Update when CC's output format changes.
created: 2026-05-24T00:00:00Z
updated: 2026-05-24T00:00:00Z
share: none
confidence: 1.0
---

# Claude Code Control

`claude_code` adapter (in Tool Agent) controls Claude Code via tmux. This file holds the
regex patterns it uses to recognise key events in Claude Code's output, and the keys it sends
back. Keep updated as Claude Code's UI evolves.

## Detection patterns

The adapter runs a polling `capture-pane` loop on each active tmux session. For each frame of
output, it tries these patterns in order:

### Permission request (→ emit `tool_reverse_wake { wake_kind: permission_request }`)

```yaml
permission_patterns:
  - 'Do you want to .*\?\s*\(y/n\)'
  - 'Approve this action\?\s*\[y/N\]'
  - 'Allow .* \[y/N\]'
  - '\? for shortcuts'    # generic CC prompt indicator
```

When matched, the adapter extracts the proposed action context (usually the previous 2–5
lines of output) and forwards in the `context` field of the reverse-wake event.

### Task complete (→ emit `RPCResult { status: success }`)

```yaml
completion_patterns:
  - '✓ Task complete'
  - '✔ Done\.'
  - 'Successfully .*'
  - '^Finished:'
```

### Error (→ emit `RPCResult { status: failure }`)

```yaml
error_patterns:
  - '^Error: '
  - '^✗ '
  - 'Traceback \(most recent call last\):'
  - 'fatal: '
```

### Draft markers (used for `result_format=draft` extraction)

When the adapter dispatches a `draft` action, it prepends a system message to the CC prompt
asking CC to output its artefact between explicit markers. The adapter extracts content
between:

```yaml
draft_markers:
  start: '<DRAFT>'
  end: '</DRAFT>'
```

If CC doesn't emit these markers (or emits tool calls before the markers, indicating it
didn't respect the draft constraint), the adapter returns `RPCResult { status: failure,
diagnostics: "CC did not respect draft mode" }`.

## Response keys

When Cortex receives a user_decision for a reverse-wake permission_request, it dispatches
`claude_code.send_keys` to push the right response back into CC's tmux session.

```yaml
keys:
  permission_grant_once:    'y\n'
  permission_grant_session: 's\n'     # if CC supports; falls back to 'y\n'
  permission_deny:          'n\n'
  abort:                    'C-c'     # Ctrl-C, sent via tmux 'send-keys' with `-l` removed
```

## Tmux session config

```yaml
tmux:
  socket_path: '/tmp/cortex-tool-agent-cc.sock'   # isolated socket per-Constellation
  session_name_format: 'cc-{rpc_id}'
  session_max_age_hours: 24
  capture_pane_lines: 200    # how many lines to capture per poll
  poll_interval_seconds: 1.0  # for active sessions
  poll_interval_seconds_paused: 30.0  # back off when paused
```

## Cleanup

```yaml
cleanup:
  kill_completed_sessions_after_seconds: 60   # one minute after success/failure
  kill_paused_sessions_after_hours: 24        # one day in paused state = abandon
```

Adapter also kills sessions on Tool Agent shutdown to avoid orphans.

## Open questions

```yaml
unknown:
  cc_session_resume_supported: false  # CC reportedly has --resume; verify in Phase 2
  cc_supports_explicit_draft_mode: false  # current workaround is via prompt prefix; check CC SDK
```

These get resolved in Phase 2 when the adapter is implemented; this file gets updated.

## You can edit this

- New CC version changes prompt format? Update patterns here, no adapter code change needed.
- Want adapter to recognise an additional event (e.g., "file_modified" prompt)? Add a pattern.

---

*The adapter loads this file on startup and re-loads if mtime changes. Hot-reloadable.*
