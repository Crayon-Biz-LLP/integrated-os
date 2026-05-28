# Integrated-OS Agent Guide

## Project Overview
FastAPI-based executive command system deployed as Vercel serverless functions (Python 3.11, matches CI). Processes Telegram messages into tasks, syncs with Google Calendar/Tasks, sends AI-generated briefings via Telegram.

## Key Commands

### Local Development
```bash
pip install -r requirements.txt
pip install uvicorn  # Not in requirements.txt
uvicorn api.index:app --reload --port 8000
```

### Pulse CLI (Local)
```bash
python core/pulse_cli.py         # Main AI briefing
python core/pulse_cli.py decisions  # Decision pulse (no AI)
# Both require PULSE_SECRET, Supabase, Gemini, Telegram vars
```

### Deployment
Vercel auto-deploys `main` branch. All routes rewritten to `api/index.py` (see `vercel.json`). Serverless function timeout: 60s.

## Architecture

### Entry Points
- `api/index.py:29` - POST `/api/webhook` - Telegram message intake
- `api/index.py:44` - POST `/api/pulse` - Scheduled briefing engine
- `api/index.py:318` - POST `/api/whatsapp-ingest` - WhatsApp notification ingest (MacroDroid)
- `core/pulse_cli.py` - CLI entry for pulse (used in CI)

### Core Modules
- `core/webhook/handler.py` - Telegram command handling, raw dump capture, message classification
- `core/pulse/engine.py` - AI briefing generation (`process_pulse`), task management, calendar sync, and **Decision Pulse** (`process_decision_pulse` — no AI, just pending email/call/whatsapp items). `format_rfc3339()` in `core/services/google_service.py`
- `core/agents/research_agent.py` - Research and embedding tasks
- `core/skills/` - Ingest (email, archive) and graph sync scripts (run via CI)

### Database (Supabase)
- Uses `SUPABASE_SERVICE_ROLE_KEY` (bypasses RLS)
- Tables: `tasks`, `raw_dumps`, `memories`, `graph_nodes`, `graph_edges`, `projects`, `resources`, `missions`, `people`, `core_config`, `whatsapp_messages`
- **Note**: `raw_dumps` does NOT store embeddings - only `memories` table has embeddings
- `whatsapp_messages` holds WhatsApp chats with classification + approval status
- `backfill_graph.py` syncs graph edges from memories (has LLM fallback: Gemini → Gemma → OpenRouter)

### External Integrations
- **Gemini AI**: Briefing (`gemini-3.5-flash`), Classification (`gemini-3.1-flash-lite`), Embeddings (`gemini-embedding-2-preview`)
- **Native Control Layer**: Built-in to `core/pulse/llm.py` to enforce Pydantic JSON validation, targeted prompt mutations, and jittered exponential backoffs.
- Google Calendar API (event blocks), Google Tasks API (checklist)
- Telegram Bot API
- WhatsApp via MacroDroid (Android notification → webhook to `/api/whatsapp-ingest`)

### Shortcode Prefixes
| Prefix | Table | Action |
|--------|-------|--------|
| `e{id}` | `email_pending_tasks` | Approve/reject email-suggested task |
| `c{id}` | `call_pending_items` | Approve/reject call-extracted item |
| `w{id}` | `whatsapp_messages` | Approve/reject WhatsApp-suggested task |
| `{id}` (bare) | Tries email → call → whatsapp → practice | Fallback compat |

## Project Routing Tags
| Tag | Purpose |
|-----|---------|
| SOLVSTRAT | Client services & delivery |
| QHORD | Product GTM & launch (June 2026) |
| ASHRAYA | Church admin, operations, finances |
| PERSONAL | Family, home, health, spiritual, journaling |
| CRAYON | Company governance, legal, tax, umbrella entity |

## Critical Conventions

### Time Handling
- All timestamps use **IST (UTC+05:30)**
- Use `format_rfc3339()` in `core/services/google_service.py` to sanitize times
- Format: `YYYY-MM-DDTHH:MM:SS+05:30`

### Security
- Pulse endpoints validate `PULSE_SECRET` (header `x-pulse-secret`) and HMAC `X-Rhodey-Signature`
- Frontend-facing endpoints (`/api/messages`, `/api/calendar-events`, `/api/tasks/*`, `/api/send-message`, `/api/send-draft`, `/api/email-action`) require `X-API-Key` header matching `API_SECRET_KEY` (constant-time comparison via `hmac.compare_digest`). No auth on `/api/webhook`, `/api/pulse` (has its own), or `/` health check.
- Supabase uses service role key (bypasses RLS)

### Pulse Cron Schedule (UTC, matches `.github/workflows/pulse.yml`)
- **Main briefing**: Weekdays `30 23 * * 1-5` + `0 2,6,9,12 * * 1-5` (5AM, 7:30AM, 11:30AM, 2:30PM, 5:30PM IST); Weekends `30 2,9 * * 0,6` (8AM, 3PM IST)
- **Decision Pulse** (no AI, pending approvals): `30 4,10 * * *` (10AM, 3:30PM IST) — runs alongside main briefing on overlapping crons

### AI Briefing Rules
- NEVER create tasks from URLs unless explicitly commanded
- NEVER mark tasks done unless input explicitly matches
- Return empty arrays if no explicit commands in inputs
- Filter tasks by 2-day horizon, 14-day creation window

### Data Deletion Safety (Non-Negotiable)
- **NEVER delete any database records (people, tasks, graph_nodes, etc.) without explicit user approval.** Present what would be deleted and ask before executing.
- This applies to: `DELETE` queries, marking records as pruned/removed, and cascade deletions.
- Always use `--dry-run` mode first and show the user what will be affected before running destructive operations.

### Code Quality (Ruff)
- **Always run `ruff check .` after making any code changes.** This project uses `ruff` to catch undefined variables, missing imports, and other linting errors before they crash in production. Fix any *newly introduced* errors immediately.

### Product Summary (Living Documentation — Non-Negotiable)
- `product-summary/` must stay in sync with the codebase **in every commit**, not as a follow-up
- When modifying existing behavior → update the relevant `product-summary/XX-<topic>.md` file in the same commit
- When adding a new feature or solution → create a new file AND update `product-summary/README.md` contents table **in the same commit**
- Violation example: The completion handler (`completion_handler.py`) was committed without any `product-summary/` update — it went undocumented for weeks. This is now explicitly forbidden by `.speckit/speckit.constitution.md` §7 criterion #6 (Documentation is part of "Done")
- `.speckit/` artifacts (plan, tasks, spec, analyze) must also be reviewed and updated if the change affects architecture, task backlog, or cross-artifact consistency

### CodeGraph Pre-Flight (Non-Negotiable)
Before implementing any bug fix, behavior change, or new feature, the agent MUST run these three queries in order:

1. **`codegraph_context("<problem description>")`** — maps all relevant files, symbols, and entry points in one view, preventing single-file tunnel vision
2. **`codegraph_trace("<entry point>", "<target data/table>")`** — traces the full data flow from ingestion to storage, surfacing ALL paths (not just the symptomatic one)
3. **`codegraph_impact("<symbol to change>")`** — reveals what other code depends on the symbol being modified

Violation example: Yesterday's URL→TASK fix touched only `engine.py` because the symptom appeared there. `codegraph_trace("webhook_route", "raw_dumps")` would have revealed `dispatch.py:handle_confident_note` as the primary ingestion path, preventing the oversight.

## Required Environment Variables
```
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
GEMINI_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
PULSE_SECRET
GOOGLE_REFRESH_TOKEN
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
GOOGLE_SHEET_ID  # Used in archive ingest and pulse
OPENROUTER_API_KEY  # Fallback for LLM calls (backfill_graph, pulse)
OPENROUTER_BASE_URL  # Default: https://openrouter.ai/api/v1/chat/completions
PULSE_HTTP_REFERER  # Default: http://localhost:8000
PULSE_APP_NAME  # Default: Pulse
API_SECRET_KEY  # Shared secret for frontend API auth (X-API-Key header)
WHATSAPP_INGEST_SECRET  # Shared secret for WhatsApp ingest (X-Ingest-Secret header)
```

## Integrated AI Tooling

This project is augmented with two local, persistent AI tools that opencode (and its sub-agents) should actively use:

### 1. CodeGraph (Semantic Code Search)
* **Purpose:** Provides instantaneous, AST-aware knowledge graph querying of the codebase.
* **Usage:** Prefer `codegraph_*` MCP tools over raw `grep` or `read` when exploring the codebase.
* **Key Tools:** 
  * `codegraph_context`: Use first to map a task or feature.
  * `codegraph_trace`: Use to understand how a request flows from point A to point B.
  * `codegraph_explore`: Use to view the source code of multiple related symbols in one call.
* **Status:** Initialized locally in the `.codegraph/` directory.

### 2. AgentMemory (Persistent Session Memory)
* **Purpose:** Retains context, user preferences, past debugging steps, and architectural decisions across different terminal sessions.
* **Usage:** Automatically captures tool usage and outputs. The agent should proactively use `memory_smart_search` to recall past context instead of asking the user to re-explain the stack.
* **Status:** Runs persistently as a local PM2 background process (`agentmemory-server`) on `http://localhost:3111`. (Real-time viewer available at `http://localhost:3113`).

## Testing
- CI: GitHub Actions (`workflow_dispatch` in `.github/workflows/pulse.yml`)
- Local: Send POST to `/api/pulse` with header `x-pulse-secret: <PULSE_SECRET>`
- No linters/typecheckers configured; skip lint/typecheck steps

## Vercel Deployment Safety

### Two Projects, Separate Config
This repo has **two Vercel projects** linked to the same GitHub repo:
- **`integrated-os`** (backend): Root Directory = `.`, Python FastAPI, uses root `vercel.json` with `rewrites` + `functions`
- **`integrated-os-frontend`** (frontend): Root Directory = `frontend/`, Next.js, no `vercel.json` (auto-detected)

**Important**: `API_SECRET_KEY` must be set as an environment variable in **both** Vercel projects — the backend reads it for auth, and the frontend proxies need it to forward the `X-API-Key` header.

### Critical: `routes` vs `rewrites` in `vercel.json`
- `routes` = **platform-level** — applied globally to ALL projects in the repo. Changes here can break other projects.
- `rewrites` = **build-level** — scoped to the project's build output. Safe to use per project.

**Rule**: Always use `rewrites` (not `routes`) in `vercel.json`. A catch-all `routes` pattern broke the frontend by routing all requests to `api/index.py` across both projects.

### Preview Deployments for Changes
Before pushing to `main`, use branch deployments to test changes without breaking production:
```bash
git checkout -b feat/my-change
# make changes, commit, push
git checkout main
# Vercel auto-deploys preview URL for the branch
```
This applies to: `vercel.json` changes, env vars, build config, framework upgrades.

### One Config Per Project Principle
- **Backend config**: root `vercel.json` (uses `rewrites` + `functions` for Python runtime)
- **Frontend**: No `vercel.json` needed (Next.js auto-detected), or its own `frontend/vercel.json`
- Never share `routes` across projects — they're platform-level, not project-level

### Safe Deployment Checklist
When making infrastructure changes:
1. [ ] Does this modify `vercel.json`, `.vercelignore`, or build config?
2. [ ] Have I checked what other Vercel projects share this repo?
3. [ ] Could `routes` or `builds` affect other projects?
4. [ ] Use a preview/branch deployment to test first
5. [ ] Check build logs for warnings (e.g., "builds existing in config" warning)
6. [ ] Verify both frontend AND backend still work after deployment

## Agent Skills Configuration (Matt Pocock Skills & General)

**For all skills (like /diagnose, /tdd, /to-prd, /to-issues, /grill-with-docs), strictly adhere to the following project conventions:**

1. **Issue Tracker & Workflow (Conversational + Spec-Kit):** 
   - **DO NOT** ask for GitHub Issue numbers, Jira tickets, or Linear links.
   - The user's primary bug-reporting workflow is conversational (via chat logs).
   - When the user triggers `/to-prd` or `/to-issues`, **DO NOT** create a GitHub issue. Instead, write the resulting spec or tasks directly into `.speckit/speckit.specify.md` and `.speckit/speckit.tasks.md`.

2. **Debugging Process (/diagnose):**
   - When the user pastes an error log (e.g., from `audit_logs` in Supabase), immediately jump to the "Hypothesize" and "Verify" steps.
   - You MUST use the `CodeGraph` tool (`codegraph_trace`, `codegraph_context`) to verify your hypothesis before proposing a fix.

3. **Domain Documentation & ADRs:**
   - The "Shared Domain Language" and high-level "Architectural Decisions" are stored in the `product-summary/` folder.
   - When using `/grill-with-docs` or discussing new features, validate your assumptions against the `product-summary/` markdown files.
   - Propose updates to the relevant `product-summary/` file rather than creating a new `docs/adr/` folder.

4. **Session Memory:**
   - Use the `agentmemory` MCP tool to recall past context and save new architectural decisions across sessions so the user doesn't have to repeat themselves.

## Spec-Driven Development (Spec Kit)

Uses [github/spec-kit](https://github.com/github/spec-kit) for structured AI-assisted development.

### Directory Structure
- `.specify/` — spec-kit CLI config (templates, scripts, workflows, extensions). Managed via `specify init/add/remove`.
- `.speckit/` — Manually-authored SDD artifacts (constitution, spec, plan, tasks, analyze). Source of truth for governance and specs.
- `.opencode/command/speckit.*.md` — Slash commands available to the opencode agent.

### Key Reference Files
| File | When to Read |
|---|---|
| `.speckit/speckit.constitution.md` | **Always** — non-negotiable project rules |
| `.speckit/speckit.plan.md` | Before any architecture/stack decision |
| `.speckit/speckit.specify.md` | When implementing a new feature |
| `.speckit/speckit.tasks.md` | When picking up implementation work |
| `.speckit/speckit.analyze.md` | Before writing code — cross-artifact contradictions |

### Available Slash Commands
`/speckit.constitution`, `/speckit.specify`, `/speckit.plan`, `/speckit.tasks`, `/speckit.implement`, `/speckit.analyze`, `/speckit.clarify`, `/speckit.checklist`, `/speckit.taskstoissues`

### Investigation Safety Rule (Non-Negotiable)
- **NEVER create temporary files, test scripts, or folders for investigation or execution without explicit prior user approval.** Do not create anything without explicit permission from the user.

### Git Safety Rule (Non-Negotiable)
- **NEVER auto-commit or auto-push changes.** Always present a summary of changes and wait for explicit user approval before any `git add`, `git commit`, or `git push`.
- The git extension hooks are configured with `auto_commit: default: false` — if an agent prompt asks about committing, say no and let the user decide.
- Branch creation (`speckit.git.feature`) is acceptable without approval since it does not create commits.

### CLI
```bash
specify check  # Verify spec-kit tooling is ready
```

<!-- SPECKIT START -->
For additional context about technologies to be used, project structure,
shell commands, and other important information, read the current plan
<!-- SPECKIT END -->


<!-- SKILLKIT_START -->
# Skills

The following skills are available to help complete tasks:

<skills>
<skill>
<name>karpathy-guidelines</name>
<description>Behavioral guidelines to reduce common LLM coding mistakes. Use when writing, reviewing, or refactoring code to avoid overcomplication, make surgical changes, surface assumptions, and define verifiable success criteria.</description>
<location>project</location>
</skill>
</skills>

## How to Use

When a task matches a skill's description:

```bash
skillkit read <skill-name>
```

This loads the skill's instructions into context.

<!-- SKILLKIT_END -->

<!-- SKILLKIT_START -->
# Skills

The following skills are available to help complete tasks:

<skills>
<skill>
<name>karpathy-guidelines</name>
<description>Behavioral guidelines to reduce common LLM coding mistakes. Use when writing, reviewing, or refactoring code to avoid overcomplication, make surgical changes, surface assumptions, and define verifiable success criteria.</description>
<location>project</location>
</skill>
</skills>

## How to Use

When a task matches a skill's description:

```bash
skillkit read <skill-name>
```

This loads the skill's instructions into context.

<!-- SKILLKIT_END -->

<!-- SKILLKIT_START -->
# Skills

The following skills are available to help complete tasks:

<skills>
<skill>
<name>karpathy-guidelines</name>
<description>Behavioral guidelines to reduce common LLM coding mistakes. Use when writing, reviewing, or refactoring code to avoid overcomplication, make surgical changes, surface assumptions, and define verifiable success criteria.</description>
<location>project</location>
</skill>

<skill>
<name>supabase</name>
<description>Use when doing ANY task involving Supabase. Triggers: Supabase products (Database, Auth, Edge Functions, Realtime, Storage, Vectors, Cron, Queues); client libraries and SSR integrations (supabase-js, @supabase/ssr) in Next.js, React, SvelteKit, Astro, Remix; auth issues (login, logout, sessions, JWT, cookies, getSession, getUser, getClaims, RLS); Supabase CLI or MCP server; schema changes, migrations, security audits, Postgres extensions (pg_graphql, pg_cron, pg_vector).</description>
<location>project</location>
</skill>

<skill>
<name>supabase-postgres-best-practices</name>
<description>Postgres performance optimization and best practices from Supabase. Use this skill when writing, reviewing, or optimizing Postgres queries, schema designs, or database configurations.</description>
<location>project</location>
</skill>
</skills>

## How to Use

When a task matches a skill's description:

```bash
skillkit read <skill-name>
```

This loads the skill's instructions into context.

<!-- SKILLKIT_END -->

<!-- SKILLKIT_START -->
# Skills

The following skills are available to help complete tasks:

<skills>
<skill>
<name>caveman</name>
<description>Ultra-compressed communication mode. Cuts token usage ~75% by dropping filler, articles, and pleasantries while keeping full technical accuracy. Use when user says &quot;caveman mode&quot;, &quot;talk like caveman&quot;, &quot;use caveman&quot;, &quot;less tokens&quot;, &quot;be brief&quot;, or invokes /caveman.
</description>
<location>project</location>
</skill>

<skill>
<name>design-an-interface</name>
<description>Generate multiple radically different interface designs for a module using parallel sub-agents. Use when user wants to design an API, explore interface options, compare module shapes, or mentions &quot;design it twice&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>diagnose</name>
<description>Disciplined diagnosis loop for hard bugs and performance regressions. Reproduce → minimise → hypothesise → instrument → fix → regression-test. Use when user says &quot;diagnose this&quot; / &quot;debug this&quot;, reports a bug, says something is broken/throwing/failing, or describes a performance regression.</description>
<location>project</location>
</skill>

<skill>
<name>edit-article</name>
<description>Edit and improve articles by restructuring sections, improving clarity, and tightening prose. Use when user wants to edit, revise, or improve an article draft.</description>
<location>project</location>
</skill>

<skill>
<name>git-guardrails-claude-code</name>
<description>Set up Claude Code hooks to block dangerous git commands (push, reset --hard, clean, branch -D, etc.) before they execute. Use when user wants to prevent destructive git operations, add git safety hooks, or block git push/reset in Claude Code.</description>
<location>project</location>
</skill>

<skill>
<name>grill-me</name>
<description>Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions &quot;grill me&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>grill-with-docs</name>
<description>Grilling session that challenges your plan against the existing domain model, sharpens terminology, and updates documentation (CONTEXT.md, ADRs) inline as decisions crystallise. Use when user wants to stress-test a plan against their project&apos;s language and documented decisions.</description>
<location>project</location>
</skill>

<skill>
<name>handoff</name>
<description>Compact the current conversation into a handoff document for another agent to pick up.</description>
<location>project</location>
</skill>

<skill>
<name>improve-codebase-architecture</name>
<description>Find deepening opportunities in a codebase, informed by the domain language in CONTEXT.md and the decisions in docs/adr/. Use when the user wants to improve architecture, find refactoring opportunities, consolidate tightly-coupled modules, or make a codebase more testable and AI-navigable.</description>
<location>project</location>
</skill>

<skill>
<name>karpathy-guidelines</name>
<description>Behavioral guidelines to reduce common LLM coding mistakes. Use when writing, reviewing, or refactoring code to avoid overcomplication, make surgical changes, surface assumptions, and define verifiable success criteria.</description>
<location>project</location>
</skill>

<skill>
<name>migrate-to-shoehorn</name>
<description>Migrate test files from `as` type assertions to @total-typescript/shoehorn. Use when user mentions shoehorn, wants to replace `as` in tests, or needs partial test data.</description>
<location>project</location>
</skill>

<skill>
<name>obsidian-vault</name>
<description>Search, create, and manage notes in the Obsidian vault with wikilinks and index notes. Use when user wants to find, create, or organize notes in Obsidian.</description>
<location>project</location>
</skill>

<skill>
<name>prototype</name>
<description>Build a throwaway prototype to flesh out a design before committing to it. Routes between two branches — a runnable terminal app for state/business-logic questions, or several radically different UI variations toggleable from one route. Use when the user wants to prototype, sanity-check a data model or state machine, mock up a UI, explore design options, or says &quot;prototype this&quot;, &quot;let me play with it&quot;, &quot;try a few designs&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>qa</name>
<description>Interactive QA session where user reports bugs or issues conversationally, and the agent files GitHub issues. Explores the codebase in the background for context and domain language. Use when user wants to report bugs, do QA, file issues conversationally, or mentions &quot;QA session&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>request-refactor-plan</name>
<description>Create a detailed refactor plan with tiny commits via user interview, then file it as a GitHub issue. Use when user wants to plan a refactor, create a refactoring RFC, or break a refactor into safe incremental steps.</description>
<location>project</location>
</skill>

<skill>
<name>review</name>
<description>Review the changes since a fixed point (commit, branch, tag, or merge-base) along two axes — Standards (does the code follow this repo&apos;s documented coding standards?) and Spec (does the code match what the originating issue/PRD asked for?). Runs both reviews in parallel sub-agents and reports them side by side. Use when the user wants to review a branch, a PR, work-in-progress changes, or asks to &quot;review since X&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>scaffold-exercises</name>
<description>Create exercise directory structures with sections, problems, solutions, and explainers that pass linting. Use when user wants to scaffold exercises, create exercise stubs, or set up a new course section.</description>
<location>project</location>
</skill>

<skill>
<name>setup-matt-pocock-skills</name>
<description>Sets up an `## Agent skills` block in AGENTS.md/CLAUDE.md and `docs/agents/` so the engineering skills know this repo&apos;s issue tracker (GitHub or local markdown), triage label vocabulary, and domain doc layout. Run before first use of `to-issues`, `to-prd`, `triage`, `diagnose`, `tdd`, `improve-codebase-architecture`, or `zoom-out` — or if those skills appear to be missing context about the issue tracker, triage labels, or domain docs.</description>
<location>project</location>
</skill>

<skill>
<name>setup-pre-commit</name>
<description>Set up Husky pre-commit hooks with lint-staged (Prettier), type checking, and tests in the current repo. Use when user wants to add pre-commit hooks, set up Husky, configure lint-staged, or add commit-time formatting/typechecking/testing.</description>
<location>project</location>
</skill>

<skill>
<name>supabase</name>
<description>Use when doing ANY task involving Supabase. Triggers: Supabase products (Database, Auth, Edge Functions, Realtime, Storage, Vectors, Cron, Queues); client libraries and SSR integrations (supabase-js, @supabase/ssr) in Next.js, React, SvelteKit, Astro, Remix; auth issues (login, logout, sessions, JWT, cookies, getSession, getUser, getClaims, RLS); Supabase CLI or MCP server; schema changes, migrations, security audits, Postgres extensions (pg_graphql, pg_cron, pg_vector).</description>
<location>project</location>
</skill>

<skill>
<name>supabase-postgres-best-practices</name>
<description>Postgres performance optimization and best practices from Supabase. Use this skill when writing, reviewing, or optimizing Postgres queries, schema designs, or database configurations.</description>
<location>project</location>
</skill>

<skill>
<name>tdd</name>
<description>Test-driven development with red-green-refactor loop. Use when user wants to build features or fix bugs using TDD, mentions &quot;red-green-refactor&quot;, wants integration tests, or asks for test-first development.</description>
<location>project</location>
</skill>

<skill>
<name>to-issues</name>
<description>Break a plan, spec, or PRD into independently-grabbable issues on the project issue tracker using tracer-bullet vertical slices. Use when user wants to convert a plan into issues, create implementation tickets, or break down work into issues.</description>
<location>project</location>
</skill>

<skill>
<name>to-prd</name>
<description>Turn the current conversation context into a PRD and publish it to the project issue tracker. Use when user wants to create a PRD from the current context.</description>
<location>project</location>
</skill>

<skill>
<name>triage</name>
<description>Triage issues through a state machine driven by triage roles. Use when user wants to create an issue, triage issues, review incoming bugs or feature requests, prepare issues for an AFK agent, or manage issue workflow.</description>
<location>project</location>
</skill>

<skill>
<name>ubiquitous-language</name>
<description>Extract a DDD-style ubiquitous language glossary from the current conversation, flagging ambiguities and proposing canonical terms. Saves to UBIQUITOUS_LANGUAGE.md. Use when user wants to define domain terms, build a glossary, harden terminology, create a ubiquitous language, or mentions &quot;domain model&quot; or &quot;DDD&quot;.</description>
<location>project</location>
</skill>

<skill>
<name>write-a-skill</name>
<description>Create new agent skills with proper structure, progressive disclosure, and bundled resources. Use when user wants to create, write, or build a new skill.</description>
<location>project</location>
</skill>

<skill>
<name>writing-beats</name>
<description>Shape an article as a journey of beats, choose-your-own-adventure style. The user picks a starting beat from the raw material, you write only that beat, then offer options for where to pivot next, beat by beat, until the article reaches a natural end. Use when the user has raw material and wants to assemble it as a narrative rather than an argument.</description>
<location>project</location>
</skill>

<skill>
<name>writing-fragments</name>
<description>Grilling session that mines the user for fragments — heterogeneous nuggets of writing (claims, vignettes, sharp sentences, half-thoughts) — and appends them to a single document as raw material for a future article. Use when the user wants to develop ideas before imposing structure, or mentions &quot;fragments&quot;, &quot;ideate&quot;, or &quot;raw material&quot; for writing.</description>
<location>project</location>
</skill>

<skill>
<name>writing-shape</name>
<description>Take a markdown file of raw material and shape it into an article through a conversational session — drafting candidate openings, growing the piece paragraph by paragraph, arguing about format (lists, tables, callouts, quotes) at each step. Use when the user has a pile of notes, fragments, or a rough draft and wants help turning it into something publishable.</description>
<location>project</location>
</skill>

<skill>
<name>zoom-out</name>
<description>Tell the agent to zoom out and give broader context or a higher-level perspective. Use when you&apos;re unfamiliar with a section of code or need to understand how it fits into the bigger picture.</description>
<location>project</location>
</skill>
</skills>

## How to Use

When a task matches a skill's description:

```bash
skillkit read <skill-name>
```

This loads the skill's instructions into context.

<!-- SKILLKIT_END -->