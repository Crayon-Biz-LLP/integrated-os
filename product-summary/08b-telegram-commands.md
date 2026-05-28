# 8b. Telegram Command Reference

All commands available via the Telegram bot, organized by function.

## Briefings & Status

### `/brief`
Triggers an off-schedule Pulse briefing via GitHub Actions workflow dispatch. Runs the full cycle: archive ingest → graph backfill → Gemini briefing → Telegram delivery.

### `/today` or `/day`
Instant daily brief without the full Pulse cycle. Queries Google Calendar + Outlook Calendar for today's events, fetches active tasks, and sends a Gemini-generated summary. The only difference from the scheduled Pulse is the absence of hindsight/staging context — it's a fast calendar + task overview.

### `/status`
Board snapshot returning:
- Counts of urgent / important / stale tasks
- Pending email decisions (`/ep`) and pending drafts (`/ed`) counts
- Queue depth (staged dumps, failed queue items)
- Links to `/library` and `/ep` for detail

### `/urgent`
Shows the single highest-priority task (status='todo', priority='urgent', sorted by created_at ascending, oldest first). If none, returns "Nothing urgent right now."

## Practices & Missions

### `/practices`
Practice health dashboard. Returns a formatted list of all active/dormant practices with visual health bars (e.g., `███░ 70%`), trend arrows (↑/↓/→), and frequency observations. Also shows drift indicators and correlation data if available.

### `/mission [goal]`
Declares a new mission. Creates a `graph_nodes` entry with type='mission' after deduplication check (ilike label match). If a matching mission exists, returns the existing mission ID. Otherwise inserts and returns the new mission ID.

### `/season [text]`
Sets or views the strategic season context. With text: writes to `core_config` under key `season_context`. Without text: returns the current season context including expiry (if set) and days remaining. See [Governance & Security §Strategic Season](23-governance-security.md).

### `/drop-<practice>`
Dismisses a detected or declared practice. Writes a suppression entry to `core_config` (key: `suppressed_practice_variants`) with variants of the practice name. Future embedding-based detection will skip these variants. See [Practices & Rhythms](19-practices-rhythms.md).

## Knowledge & Resources

### `/library`
Returns the last 10 enriched resources with title, category, and linked mission. Resources are enriched by the Pulse engine (mission backfill, entity linking).

### `/vault`
Returns a link to the Streamlit command center (external tool). Used for deeper data inspection, raw queries, and administrative operations beyond the Telegram and web interfaces.

### `?query`
Brain interrogation. Performs a hybrid search: graph traversal + vector memory search (`match_memories` RPC) + canonical page search (`match_canonical_pages` RPC) + resource search + task context. All signals combined and sent to Gemini for synthesis. See [Memory & Knowledge Graph](16-memory-knowledge-graph.md) and [Canonical Brain Synthesis](17-canonical-brain-synthesis.md).

## Email Decisions

### `/ep`
Lists all pending email decisions (`email_pending_tasks` where `danny_decision IS NULL`). Each shown with a shortcode number and task title. Reply `"5 yes"` to approve or `"5 drop"` to reject. See [Email Pipeline §Pending Task Approval](20-email-pipeline.md).

### `/ed`
Lists all pending email drafts with email context (sender, subject). Reply `"ed approve <id>"`, `"ed reject <id>"`, or `"ed edit <id> <new text>"` to manage. See [Email Pipeline §Managing Drafts](20-email-pipeline.md).

## Undo System

### `/undo` (bare)
Shows the most recent user entry (from `raw_dumps`) with its current type (task/note). Prompts for action: `'t'` (keep task), `'n'` (flip to note), `'d'` (delete).

### `/undo n`
Flips the last entry to a note. Cancels any matching task (dedup_key match), generates an embedding, saves to `memories` as `memory_type='note'` with `source='webhook_undo'`.

### `/undo t`
Flips the last entry to a task. Reverts it to pending status for inline processing.

### `/undo d`
Soft deletes the last entry. Cancels matching tasks (marks as cancelled), marks the raw_dump as completed with no further processing.

## Fast Commands (Non-Classified Input)

### `N:` or `Note:` prefix
Skips Gemini intent classification entirely. The message is directly routed to note creation: embedded and saved to `memories` as `memory_type='note'`. Bypasses the full 6-stage pipeline for speed. See [Telegram Intake §Stage 6](06-telegram-intake.md).
