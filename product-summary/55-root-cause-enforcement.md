# 4W1H Root Cause Enforcement

## Problem

The root cause investigation procedure in AGENTS.md was advisory. The agent could propose quick-fix bandaids, skip the 9-step investigation, and commit without documenting why the fix was the actual root cause. No enforcement existed.

## Solution

Three-layer enforcement:

### Layer 1 — Git Hook (`.githooks/commit-msg`)

Shell hook that reads the commit message and rejects if it lacks a `Root Cause:` line. Skips merge and squash commits. Bypassable via `git commit --no-verify` (explicit opt-out). Already active project-wide via `git config core.hooksPath .githooks`.

### Layer 2 — Agent Procedure (AGENTS.md)

Step 10 added to the Root Cause Investigation Procedure: "Document the 4W1H". Specifies the 4W1H format for commit messages:

```
Root Cause: <why the bug happened — chain of events, not what you changed>
What:       <what the fix does at the code level>
Where:      <which files, functions, lines>
When:       <reproduction conditions>
How:        <how this prevents recurrence>
```

The `Root Cause:` line is enforced by the hook. The other 4 fields are strongly recommended for non-trivial fixes. Purely additive docs/config can use `Root Cause: N/A — docs/config update`.

### Layer 3 — Config (opencode.json)

`"commit"` section documents the requirement at the agent config level for visibility.

## Chain of Enforcement

1. Bug reported → AGENTS.md loads → 9-step procedure + 4W1H required
2. Agent investigates (diagnose skill for complex bugs)
3. Agent writes commit → `commit-msg` hook checks for `Root Cause:` line
4. Missing? Rejected with error message showing 4W1H format
5. No way to accidentally skip — hook must be explicitly bypassed

## Key Files
- `.githooks/commit-msg` — Commit message hook
- `AGENTS.md` — Step 10: 4W1H documentation requirement
- `opencode.json` — Config-level documentation of commit requirements
