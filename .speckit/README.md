# Rhodey OS — Spec Kit Files

This directory contains the Spec-Driven Development (SDD) documents for Rhodey OS,
generated using the [GitHub Spec Kit](https://github.com/github/spec-kit) methodology.

## How to Use These Files

1. Add this directory to the root of your `integrated-os` repository as `.speckit/`
2. When working with an AI coding agent (Gemini CLI, Cursor, Claude Code):
   - Reference `speckit.constitution.md` first — these are the non-negotiable rules
   - Reference `speckit.specify.md` for what you are building
   - Reference `speckit.tasks.md` for the ordered task list
   - Use `speckit.analyze.md` to catch contradictions before writing code

## File Index

| File | Purpose |
|---|---|
| `speckit.constitution.md` | Governing principles — never broken |
| `speckit.specify.md` | Feature specifications (SPEC-001 to SPEC-005) |
| `speckit.plan.md` | Architecture decisions and stack rationale |
| `speckit.tasks.md` | Ordered implementation task list (T-001 to T-012) |
| `speckit.analyze.md` | Cross-artifact consistency checks |

## Where to Start

**Right now, this week:**
1. Read `speckit.tasks.md` → Start at T-001
2. T-001 through T-005 are the "stop the bleeding" tasks
3. None of them break existing functionality — all additive

**Do NOT start T-007 (backfill) until T-006 has been stable for 48 hours.**

