> **⚠️ LEGACY WARNING**: This file references architecture from an earlier phase. Some modules mentioned (e.g., handle_confident_task, process_single_dump, quick_process, janitor) have been replaced or removed in Parts 57-61. The core concept remains valid — see 58-final-architecture-overhaul.md for current architecture.
# 24. Use Cases & End-to-End Workflows

## Capture & Intake (5 Use Cases)

### 1. Voice Note → Calendar Event in Seconds

**Scenario**: Danny is walking out of a meeting and remembers he needs to prepare Q3 pricing for a client. He opens Telegram, holds the voice button, and says: *"Prepare the Q3 pricing proposal for Solvstrat client — needs to be done by Friday 3pm."*

**What happens**:
1. Telegram sends the voice message to the webhook
2. `multimodal.py` downloads the audio, sends it to Gemini for transcription
3. Gemini returns: `{"intent": "TASK", "title": "Prepare Q3 pricing proposal", "entity": "SOLVSTRAT", "time_context": "Friday 3pm"}`
4. `handle_confident_task()` inserts into raw_dumps
5. `plan_actions()` → `execute_planned_actions()` runs inline: creates task in DB, syncs to Google Calendar (Friday 3pm), syncs to Google Tasks
6. Telegram receipt: *"Task logged."* (stealth — no entity mentioned)

**Time**: ~3-5 seconds from voice memo to Google Calendar event.

### 2. Photo of a Whiteboard → 3 Structured Tasks

**Scenario**: After a brainstorming session, Danny snaps a photo of the whiteboard and sends it to Telegram.

**What happens**:
1. Photo sent to Gemini with the multimodal extraction prompt
2. Gemini OCR extracts handwritten text and identifies action items
3. Returns 3 tasks with entities and time contexts
4. Each task goes through the standard creation pipeline
5. Receipt: *"Logged 3 Tasks & 2 Insights."*

### 3. PDF Meeting Notes → Research Dossier

**Scenario**: Danny receives a PDF with competitor analysis notes. He forwards it to the Telegram bot.

**What happens**:
1. Document (PDF) sent to Gemini for extraction
2. If the content suggests deeper research is needed, Gemini classifies it as DELEGATE
3. `agent_queue` gets a new entry: *"Research the pricing models mentioned in the competitor analysis"*
4. On the next `research_worker.yml` run (2x daily): Jina AI searches the web → Gemini synthesizes a dossier → stored in raw_dumps → shown in next briefing

### 4. Journal Entry → Briefing Insight

**Scenario**: Danny fills out his evening journal in Google Forms: *"Feeling drained today. Faith score: 4. Work was heavy but Sunju helped with the Qhord presentation."*

**What happens**:
1. On the next Pulse run, `archive_ingest.py` picks up the new journal row
2. Content synthesized: topic + thoughts + takeaway
3. Embedded and stored as `memory_type='archive'` with metadata: `faith_score=4, emotional_state=tired`
4. `graphify()` scans text: finds "Sunju" + "Qhord" → creates `Danny --relates_to--> Sunju` and `Danny --works_at--> Qhord` edges
5. `retrieve_hindsight_memories()` finds this entry during Pulse context building
6. Briefing COMPASS opening: *"Drained day yesterday, but that Qhord work with Sunju is building momentum. Let's look at the board."*

### 5. Incoming Email → Pending Task (Human-in-the-Loop)

**Scenario**: A client emails Danny: *"Can we get a revised quote for the Q3 engagement?"*

**What happens**:
1. `email_ingest.py` (runs 30 min before each pulse) fetches the email via Gmail API
2. Gemini classifies: `{"classification": "actionable", "suggested_task": "Revise Q3 quote for Solvstrat client", "linked_project_name": "Solvstrat", "needs_draft": true}`
3. Duplicate guard checks: no existing matching task → clean insert
4. `messages` row created: *"Revise Q3 quote for Solvstrat client"* → project: Solvstrat
5. Draft generated for the reply
6. Danny sees it in the next Decision Pulse message: *"📨 [e5] Revise Q3 quote for Solvstrat client"*
7. He replies: *"e5 yes"*
8. Task created, calendar blocked, Google Tasks synced, quote revision begins

## Decision & Command (6 Use Cases)

### 6. Approve Email Task with Shortcode

**Scenario**: Danny sees a pending task in his briefing and replies *"5 yes"*.

**What happens**:
1. Webhook matches the shortcode pattern `^(\d{1,4})\s+(yes|no|drop)$`
2. `process_email_pending_decision(5, 'approve')`:
   - Checks `is_already_in_tasks_table()` — no duplicate found
   - Inserts into raw_dumps with `source='email'`
   - Raw_dump processed by Quick Process → task created + Google sync
3. Sets `danny_decision='approved'`
4. Briefing next run: task appears in 🚀 Work section

### 7. Conflict Resolution (Update vs. Create)

**Scenario**: Danny sends: *"Follow up with Vasanth on the Q2 delivery"*

**What happens**:
1. `check_task_overlap_for_update()` finds an existing task: "Vasanth check-in"
2. ≥2 keyword match → intent is ambiguous between update and create
3. `ask_task_update_confirmation()` sends: *"This looks like it relates to: 'Vasanth check-in'. Reply 'u' to update or 'n' for new."*
4. Danny replies *"u"* → `resolve_task_update_confirmation()` merges the intent into the existing task
5. OR Danny replies *"n"* → new task created independently

### 8. The Undo System

**Scenario**: Danny accidentally sent a note as a task.

**What happens**:
1. Danny types: *"/undo"*
2. System queries the most recent user entry: *"Buy groceries"* — status: pending, type: task
3. Telegram shows: *"Undo: 'Buy groceries' (task). Reply 't' (keep task), 'n' (flip to note), 'd' (delete)."*
4. Danny types: *"n"*
5. `handle_undo_command()`:
   - Cancels the matching task (if any)
   - Generates embedding for "Buy groceries"
   - Saves to `memories` as `memory_type='note'` with `source='webhook_undo'`
   - Telegram: *"Flipped to note."*

### 9. Approve and Send Email Draft

**Scenario**: Danny sees pending drafts and wants to send one.

**What happens**:
1. Danny types: *"/ed"*
2. System lists pending drafts with email context
3. Danny types: *"ed approve 3"*
4. `handle_ed_command()`:
   - Sets status to 'sent' (anti-double-send guard)
   - If Gmail: constructs reply with In-Reply-To and References headers
   - If Outlook: calls Microsoft Graph API replyAll endpoint
   - Sends the email
5. Telegram: *"Draft sent."*

### 10. Declare a New Spiritual Practice

**Scenario**: Danny has been meditating daily and wants to track it.

**What happens**:
1. Danny sends: *"I want to start meditating daily"*
2. Gemini classifies: `DECLARE_PRACTICE` with title "Meditation"
3. `handle_declare_practice()`:
   - Generates embedding for "Meditation"
   - Compares (cosine ≥ 0.85) against all existing practice embeddings
   - No duplicate found
   - Creates `graph_nodes` entry: `type='practice'`, metadata with `health_score=100`, `frequency_observed="0/14days"`, `declared=true`
4. Telegram: *"Tracking: Meditation"*
5. Future pulses will detect occurrences, update health score, and show in /practices dashboard

## Autonomous Workflows (6 Use Cases)

### 11. Morning Pulse: Strategic Focus

**Scenario**: It's 7:30 AM IST on a weekday. The Pulse kicks off.

**What happens**:
1. GitHub Actions triggers `pulse.yml`
2. Step 1: `archive_ingest.py` — syncs any new journal entries
3. Step 2: `backfill_graph.py` — syncs graph edges from new memories
4. Step 3: `pulse_cli.py` → `engine.py`:
   - Zombie recovery: resets any stuck dumps
   - Google→Supabase sync: pulls external completions
   - Fetches all pending data + overnight Telegram messages
   - Staging sorter: classifies everything (NOTE → memory, NOISE → discard, TASK → keep)
   - Horizon guard: hides tasks >48h away, filters by 14-day creation window
   - 5 parallel AI agents run
   - 250-line prompt → Gemini generates structured JSON + briefing
   - Write phase: new tasks/projects/people created + calendars synced
5. Telegram delivers the briefing (no decision sections):
   ```
   🔴 Urgent — Revenue-critical tasks
   🚀 Work — Today's priorities (SOLVSTRAT, QHORD)
   🏠 Home — Personal items
   ✅ Done — Since last check
   ```
   Pending email/call/WhatsApp decisions arrive as a **separate Decision Pulse** message (no AI, ~2s runtime).

### 12. Afternoon Pulse: Velocity Check

**Scenario**: It's 2:30 PM IST. The execution-mode pulse runs.

**What happens**:
1. Same pipeline but persona shifts to "Afternoon Execution Mode"
2. The AI is told: *"Don't repeat strategy. Call out what's actually moving (or stalled) in the last 4 hours."*
3. Briefing is shorter — focuses on progress, blockers, and the closing window
4. Nag logic flags any urgent tasks that have been stagnant for >48h
5. Drift detection checks projects updated 3+ times in 48h
6. Compass opening: *"The Qhord pricing is the only thing that moved this afternoon. Everything else is stalled."*

### 13. Night Pulse: Audit & Archive

**Scenario**: The night pulse runs at 7:30 AM IST (next day's early morning — night mode in IST cadence).

**What happens**:
1. Persona switches to "Intel: Vaulted" mode
2. Section order flips: ✅ Done first (close loops mentally), then 🏠 Home, then 🚀 Work (top 2-3 only), then 💡 Ideas
3. After-action report runs: *"Completed 4 of 8 tasks today. Revenue-critical items all closed. Personal tasks deferred."*
4. Reflection saved as memory for future hindsight retrieval
5. Compass: *"Door closing on Tuesday. 4 loops closed, 4 carried. The Qhord quote is the one that needs your first hour tomorrow."*

### 14. Research Agent Discovers Insights

**Scenario**: Danny sends: *"Research how our competitors are pricing their AI products"*

**What happens**:
1. Gemini classifies as DELEGATE → inserted into `agent_queue` with status='pending'
2. Next `research_worker.yml` run (2x daily):
   - Picks up the queue item
   - Jina AI searches: `site:jina.ai "competitor AI pricing"`
   - Gemini synthesizes search results into a structured dossier
   - Dossier saved to `raw_dumps` with `message_type='research'`
   - Danny gets a Telegram notification: *"Research complete. Summary in next briefing."*
3. Next Pulse briefing includes the research findings

### 15. Brain Synthesis: Overnight Knowledge Consolidation

**Scenario**: Every night at 7:30 AM IST, `synthesis.yml` runs.

**What happens**:
1. Queries `canonical_pages` for entries older than 24 hours
2. For each stale page, gathers fragments from 6 sources:
   - Memories (vector search), Tasks (active + recent), Logs (AI entries), Resources (linked), Raw dumps (mentions), People (name matches)
3. Sends to Gemini with existing content + new fragments → merge command
4. Applies safety guards: minimum 300 chars, ≥60% retention ratio
5. Versioned write: old page marked `is_current=False`, new version inserted
6. Updates `last_synth_at` timestamp

### 16. Janitor: Self-Health Check

**Scenario**: Every ~3 hours, `janitor.yml` runs.

**What happens**:
1. Checks raw_dumps stuck in 'pending' or 'staged' >2 hours
2. Checks raw_dumps stuck in 'processing' >10 minutes → sends Telegram alert
3. Checks memories from last 7 days with null embeddings
4. Checks heartbeat: if `pulse_last_success` >24h old → sends warning
5. Retries failed operations from `failed_queue`
6. Logs all findings to `audit_logs`

## Knowledge Discovery (5 Use Cases)

### 17. On This Day

**Scenario**: The Pulse context-building phase includes temporal pattern detection.

**What happens**:
1. `detect_temporal_patterns()` queries memories from the same month/day across ALL years
2. Finds 3 relevant memories: "Started Qhord pricing strategy (2025)", "Sunju check-in re: church event (2024)", "Debt repayment milestone (2023)"
3. Deduplicates by content, caps at 5
4. Injected into briefing context as TEMPORAL PATTERNS
5. AI might weave it into the Compass: *"This time last year you were closing the debt repayment. Today it's about building revenue."*

### 18. Serendipity: Connecting the Dots

**Scenario**: Three things cross Danny's desk: a task "Review Qhord pricing", a resource "GTM strategy guide", and a note "Talk to Sunju about partnership models".

**What happens**:
1. Serendipity engine runs during Pulse:
   - **Layer 1**: Keyword "pricing" appears in both SOLVSTRAT task and PERSONAL note → bridge detected
   - **Layer 2**: Resource "GTM strategy guide" mentions "Sunju's framework" → person-in-resource link
   - **Layer 3**: All three were created within the same 24-hour window → temporal cluster
2. Injected into briefing context
3. AI might note: *"Pricing is surfacing across work and reading. Sunju's framework might be relevant to that Qhord project."*

### 19. Brain Interrogation

**Scenario**: Danny types: *"?what do I know about Qhord GTM"*

**What happens**:
1. `interrogate_brain()` fires:
   - Graph traversal: finds Qhord project node → follows BELONGS_TO edges → finds related tasks
   - Vector search: `match_memories` RPC with 0.5 threshold → finds relevant memories
   - Canonical page search: `match_canonical_pages` RPC with 0.65 threshold → finds Qhord master page
   - Resource search: recent resources with Qhord in title
   - Task context: active tasks mentioning Qhord
2. All signals combined into a context block
3. Gemini synthesizes: *"Qhord GTM: You have 3 active tasks (pricing review, competitor analysis, partner outreach). Your master page notes that GTM was deprioritized in Q2 but is back in focus for Q3. There's a resource on SaaS pricing models that might be useful."*

### 20. After-Action Report

**Scenario**: The night Pulse detects it's after 8 PM IST.

**What happens**:
1. `generate_after_action_report()` runs:
   - Queries: tasks completed today (4), tasks still open (3)
   - Sends to Gemini: *"Produce a dry 1-2 sentence After-Action Report"*
   - Response: *"Closed 4, carried 3. The revenue-critical items are done. Personal items deferred — move to tomorrow's top of queue."*
2. Saved as `memory_type='reflection'` with embedding
3. This reflection is retrievable via hindsight in future briefings

### 21. Season Expiry Alert

**Scenario**: Danny set a season context with `[EXPIRY: 2026-05-01]`. It's now May 2nd.

**What happens**:
1. Pulse engine checks season expiry
2. `now > expiry_date` → TRUE
3. System context set to: *"CRITICAL: Season Context EXPIRED."*
4. Briefing prompt receives this at the top
5. AI opens with: *"CRITICAL: Your strategic north star has expired. Tasks lack seasonal alignment until you update `/season`."*
6. The briefing still runs but with the strategic context flag

## Task Lifecycle (4 Use Cases)

### 22. Task Created with Time → Calendar + Tasks + Graph

**Scenario**: The Pulse AI creates a task: "Review Qhord pricing proposal" — project: Qhord, priority: urgent, due: tomorrow 2pm.

**What happens**:
1. 7-stage project cascade resolves "Qhord" → finds Qhord project ID
2. Task inserted into `tasks` table
3. `write_graph_edges_for_task()`:
   - Creates task node in graph_nodes
   - Creates BELONGS_TO edge to Qhord project node
   - Scans title for person names → creates INVOLVES edges if found
4. `sync_to_google()` creates entry in Google Tasks
5. `sync_to_calendar()` creates Google Calendar event for tomorrow 2pm
6. **De-clash check**: if another event exists at 2pm, stagger to 2:15pm
7. Task IDs stored back in DB (google_task_id, google_event_id)

### 23. Task Completed via Telegram

**Scenario**: Danny sends: *"Done with the Qhord pricing review"*

**What happens**:
1. Gemini classifies: COMPLETION (past tense detected)
2. `execute_planned_actions()` matches the dedup_key against active tasks
3. Finds matching task → marks as done
4. `delete_calendar_event()` removes the Google Calendar event
5. `sync_to_google()` marks Google Tasks as complete
6. `write_outcome_memory()` creates memory: "Completed Qhord pricing review" with project context
7. Future briefings can retrieve this outcome as hindsight

### 24. Task Completed via Web UI

**Scenario**: Danny clicks "Done" on the dashboard task card.

**What happens**:
1. PATCH `/api/tasks/{id}/status` with `{"status": "done"}`
2. If task has `google_event_id` → delete calendar event
3. If task has `google_task_id` → sync to Google Tasks
4. `versioned_update()` creates a versioned copy with status='done'
5. `write_outcome_memory()` creates outcome memory with project context
6. Dashboard auto-refreshes (SWR 30s interval)
7. Task disappears from open tasks, appears in recent completions

### 25. Task Completed Externally via Google Tasks

**Scenario**: Danny marks a task as done in Google Tasks directly.

**What happens**:
1. Next Pulse run: `sync_completed_tasks_from_google()` queries Google Tasks API
2. Finds tasks marked complete in Google but still 'todo' in Supabase
3. For each: `create_versioned_task()` in `temporal_lineage.py`:
   - Old task: `is_current=False`
   - New task: `status='done'`, `version+1`, `supersedes_id=old_id`
4. `write_outcome_memory()` creates outcome memory
5. DB is now in sync with Google's canonical state

## Practices & Rhythms (3 Use Cases)

### 26. Passive Practice Detection from Raw Text

**Scenario**: Over 3 weeks, Danny's raw_dumps show multiple entries: *"Did morning meditation"*, *"Meditated today"*, *"Skipped meditation — hectic morning"*.

**What happens**:
1. Over several Pulse runs, embedding clusters form around "meditation" content
2. Cosine similarity ≥0.75 → strong cluster
3. Cluster spans >2 weeks → pattern confirmed
4. Gemini batch verify: *"Is this a genuine recurring personal habit? → Yes, name it: 'Morning Meditation'"*
5. NOT a work entity → practice is created:
   ```python
   graph_nodes.insert({
       "label": "Morning Meditation",
       "type": "practice",
       "metadata": {
           "health_score": 70,
           "occurrence_count": 12,
           "frequency": "5/21days",
           "status": "active",
           "trend": "stable"
       }
   })
   ```
6. Next weekend pulse adds it to the rhythms dashboard

### 27. Practice Correlation with Task Completion

**Scenario**: After 20+ occurrences and 50+ completed tasks, the correlation engine runs.

**What happens**:
1. `build_practice_correlations()` compares:
   - Task completion rate on meditation days: 78%
   - Task completion rate on non-meditation days: 62%
2. Correlation detected: "positive" (+16%)
3. Weekday briefing adds: *"📊 Meditation correlates with +16% task completion."*

### 28. Practice Lifecycle: Active → Dormant → Inactive

**Scenario**: Danny stops meditating for 3 months.

**What happens**:
- **Day 0-28**: Practice remains active. Health score declines each week with no occurrences.
- **Day 29**: `status='dormant'`. Appears in "Drifting" section of /practices dashboard. Trend shows ↓.
- **Day 84**: `status='inactive'`. Variants compacted. Removed from active dashboard.
- **Future reactivation**: If Danny sends *"Meditated today"*, a new cluster forms. The inactive entry has its variants available for matching — if detected, it's reactivated rather than duplicated.

## Dashboard (2 Use Cases)

### 29. Morning Dashboard Review

**Scenario**: Danny opens the web dashboard at his desk.

**What happens**:
1. Server-side rendering fetches: open tasks (100), task stats, pending emails, email stats, pending drafts count
2. Dashboard renders:
   - **StatsCards**: "14 Open | 3 Due Today | 2 Overdue 🔴 | 5 Pending Emails 📨"
   - **WhatToDoNow**: Overdue tasks with red indicators + due-today tasks + pending email decisions with Yes/No buttons + today's calendar events
   - **QuickChat**: Last 5 messages with auto-scroll and 30s refresh
   - **PulseBriefings**: Last 3 briefings with expandable content
   - **RecentTasks**: Top 5 by due date, overdue highlighted with red border
3. Danny clicks "Done" on a completed task → PATCH API → outcome memory written → UI auto-refreshes

### 30. Exploring the Knowledge Graph

**Scenario**: Danny wants to see how "Qhord" connects to everything.

**What happens**:
1. Navigates to Memories → Knowledge Graph
2. FullGraph loads: interactive D3 force-directed visualization with ~200 nodes
3. Danny zooms in (scroll wheel), searches for "Qhord"
4. EgoGraph zooms to Qhord's ego network:
   - Qhord node (type: project) — connected to 5 task nodes via BELONGS_TO edges
   - Danny node — connected via works_at edge
   - Sunju node — connected via connected_via edge
   - 3 resource nodes linked to Qhord GTM cluster
5. Danny clicks the Sunju node → NodeFlyout slides in:
   - Type: Person
   - Connected nodes: Danny (relates_to), Qhord (connected_via), Ashraya (belongs_to)
   - 7 total connections
   - Linked canonical page: "Sunju" — last synthesized 2 days ago
6. Danny clicks the canonical page link → full content displayed in the flyout

---

## Additional Workflows

### 31. Plain Text Message → Instant Task

**Scenario**: Danny types *"Prepare slides for Monday's Ashraya board meeting"* into Telegram.

**What happens**:
1. Webhook receives the text, no shortcode match, no clarification state
2. Stage 6 intent classification: Gemini returns `{"intent": "TASK", "entity": "ASHRAYA", "confidence": 0.92}`
3. High confidence → `handle_confident_task()` inserts into raw_dumps
4. `execute_planned_actions()` runs inline: creates task in DB, syncs to Google Tasks, no time context so no calendar event
5. Telegram receipt: *"Task logged."*
6. Within 5 seconds, the task exists in both the database and Google Tasks

### 32. Quick Process: The Silent 5-Minute Worker

**Scenario**: Danny sends *"Buy milk"* and *"Call plumber"* while walking through the grocery store. Both go through the inline path, but neither creates graph edges.

**What happens**:
1. Both tasks created via `execute_planned_actions()` — immediate task in DB
2. Graph edges NOT created (Quick Process inline path does not create graph nodes — architectural constraint)
3. *(Quick Process cron removed — Action Planner handles all paths inline)* (e.g., if the webhook returned before processing finished)
5. `backfill_graph.yml` eventually catches the missing graph edges via `backfill_orphaned_tasks()`
6. By the next Pulse briefing, the graph is consistent — edges exist for both tasks

### 33. Duplicate Guard Blocks a Redundant Task

**Scenario**: Danny sends *"Follow up with Vasanth on Q3 pricing"*. A matching task already exists.

**What happens**:
1. `normalize_title()` strips punctuation, lowercases: *"follow up with vasanth on q3 pricing"*
2. `extract_core()` keeps words >3 chars: `["follow", "with", "vasanth", "pricing"]`; discards "up", "on", "q3"
3. `_extract_discriminators()` finds "Q3" → no year discriminator
4. Compares against existing task: existing = *"Follow up with Vasanth — Q3 pricing proposal"* → core words `["follow", "with", "vasanth", "pricing", "proposal"]`
5. Core word overlap: `["follow", "with", "vasanth", "pricing"]` → 4 of 5 = 80% → **BLOCK**
6. Superset check: the new title is not a superset of the existing one → no auto-merge
7. Raw_dump marked as duplicate, no task created
8. Telegram receipt: *"Task logged."* (same receipt as any success — stealth, no alert about duplicate)
9. If the system notices a pattern of repeated duplicates, the briefing AI may note the existing task's presence

### 34. The Note Bypass: Instant Filing

**Scenario**: Danny has a quick insight while driving: *"N: The Qhord GTM should emphasize the API-first approach"*

**What happens**:
1. The `N:` prefix is detected in Stage 5 (before classification)
2. Bypasses Gemini classification entirely — no intent call
3. Strips the prefix, routes directly to `handle_confident_note()`
4. Text embedded via Gemini and saved to `memories` as `memory_type='note'` with `source='webhook'`
5. Telegram receipt: *"Noted."*
6. Total round-trip: ~1.5 seconds (no classification latency)

### 35. Hindsight Retrieval: Multi-Signal Context Building

**Scenario**: It's Pulse time. The engine needs to understand recent context about Solvstrat.

**What happens**:
1. `retrieve_hindsight_memories()` fires 5 parallel queries:
   - **Vector search**: `match_memories` RPC with Solvstrat embedding → 10 memories
   - **Graph traversal**: Solvstrat project node → follows BELONGS_TO edges → 5 active tasks
   - **Canonical page**: `match_canonical_pages` RPC → Solvstrat master page (last synced yesterday)
   - **Resources**: Resources tagged with Solvstrat cluster
   - **People**: People linked to Solvstrat via works_at or connected_via edges
2. All signals aggregated into a single context block
3. Injected into the Gemini briefing prompt
4. The AI's Compass opening weaves the signal into the briefing narrative

### 36. Dashboard QuickCommand: Intent-Explicit Input

**Scenario**: Danny opens the web dashboard, clicks QuickCommand, selects "Note" mode, and types *"The team retrospective notes from Friday"*.

**What happens**:
1. Frontend sends POST to `/api/send-message` with `intent: "QUICK_COMMAND"` and `mode: "note"`
2. Backend skips classification (intent is explicit from the mode selector)
3. Message routed directly to note creation path
4. Gets embedded, saved to memories
5. Dashboard shows confirmation toast
6. No Telegram message sent — the dashboard is the interface

### 37. Monday Re-Entry with Weekend Recon

**Scenario**: It's Monday morning at 7:30 AM IST. The Pulse briefing detects it's the first briefing after the weekend.

**What happens**:
1. Pulse engine detects `datetime.now().weekday() == 0` (Monday)
2. Persona switches from Weekend mode back to Morning Strategic mode
3. Additional context injected: *"It's Monday. The user is re-entering the work week. Include a 🛡️ WEEKEND RECON section that summarizes what happened over the weekend — any new tasks created, any personal reflections, any weekend events."*
4. Briefing structure: 🛡️ Weekend Recon (first section) → 🔴 Urgent → 🚀 Work → 🏠 Home → ✅ Done
5. The Weekend Recon section shows: *"Weekend: 2 personal tasks created, 1 journal entry (faith score 8), no critical changes."*
6. Season context expiry check runs — if season is about to expire, the AI flags it in the recon

### 38. Revenue-Critical Task Stands Out in Briefing

**Scenario**: The Pulse AI creates a task *"Close Solvstrat Q3 deal — payment processing"* and flags it as revenue-critical.

**What happens**:
1. AI evaluation detects payment/sales language → sets `is_revenue_critical: true`
2. Task inserted into `tasks` table with the flag set
3. Briefing rendering: the task appears as **"Close Solvstrat Q3 deal — payment processing"** (bold)
4. Despite being in the 🚀 Work section, the bold text makes it visually distinct from non-bolded items
5. If multiple revenue-critical tasks exist, the 🔴 Urgent section gets priority in the briefing order, and the bolded items are grouped at the top within each section
