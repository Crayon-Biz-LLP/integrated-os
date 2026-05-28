# 8. Input Channels

## 5 Ways Data Enters the System

Data flows into Integrated-OS through 5 distinct channels, each with its own processing pipeline but converging into a unified data model.

## Channel 1: Telegram Capture

The primary real-time channel. Text messages, voice notes, photos, and documents arrive via Telegram Bot API webhook. Each message goes through classification → entity routing → intent dispatch. Tasks are inserted into `raw_dumps` and processed inline by `quick_process.py`. Notes are embedded and stored in `memories`. Queries trigger hybrid graph+vector search.

**Speed**: Task to Google Calendar in under 5 seconds.

## Channel 2: Google Sheets Journal Pipeline

### The Flow

Danny journals using a Google Form with 35 fields. The responses populate a Google Sheet ("Form responses 1", columns A through AI). On every Pulse run (and on-demand via JOURNAL_SYNC webhook signal), `archive_ingest.py`:

1. Fetches all new rows since last sync
2. Extracts 15+ metadata fields: emotional_state, intensity (1-10), faith_score (1-10), spillover_flag, location, category, tags, action_velocity, consistency_score, victory_flag, input_score
3. Synthesizes the content: combines topic + thoughts + takeaway + prophecy + psalm + testimony + action + prayer + sermon
4. Maps entry type via `MEMORY_TYPE_MAPPING`: Prophecy / Psalm / Journal / Prayer / Sermon
5. Generates an embedding via Gemini
6. Inserts into `memories` with `memory_type='archive'` and all metadata preserved
7. Calls `graphify()` — scans text against `ENTITY_MAPPINGS` (Solvstrat, Crayon, Qhord, Sunju, Jaden, Church) and creates typed graph edges

### Relationship Graph from Journal

When a journal entry mentions key entities, explicit graph edges are created:

| Entity | Edge | Relationship |
|--------|------|-------------|
| Sunju | Danny → Sunju | `relates_to` |
| Jaden / Jeffery / The Boys | Danny → entity | `parent_of` |
| Solvstrat / Crayon | Danny → entity | `works_at` |
| Church | Danny → Church | `belongs_to` |
| ₹30L Debt | Danny → Debt | `struggles_with` |

These edges persist in the knowledge graph and are traversed during hybrid search, serendipity detection, and briefing context building.

### From Journal to Briefing

Journal entries are NOT converted to tasks. They serve as semantic context. During the Pulse briefing, `retrieve_hindsight_memories()` performs a multi-signal vector search across all memories (including archives). The result feeds into the **COMPASS OPENING**:

> *"Weave his latest HINDSIGHT insights (Faith Score, Emotional Intensity, Takeaways, or [PROPHECY]) into the current tactical reality (Qhord, Solvstrat, Debt)."*

If hindsight is stale (>24h without new data):
> *"The signal is quiet on the reflection front, Danny. Let's look at the board."*

## Channel 3: Gmail Ingestion

Runs 30 minutes before each Pulse briefing via GitHub Actions. `email_ingest.py`:

1. Queries Gmail API for messages from the last 48 hours (labels: inbox or Completed/Ashraya)
2. Applies NOREPLY pattern filter (short-circuits to "ignored" without Gemini)
3. Fetches full message content for non-ignored emails
4. Sends to Gemini for classification:
   - `ignored`: Automated/noreply/newsletter → skip
   - `fyi`: Human sender, no response needed → add person, possibly write relationship memory
   - `actionable`: Requires response → create pending task + generate draft
5. Links sender to `people` table (with blocklist + dedup)
6. Links to `projects` table (via fuzzy ilike name match)
7. Creates `email_pending_tasks` with duplicate guard
8. Generates `email_drafts` if `needs_draft=true`

**Duplicate guard for email tasks**: Uses the same three-tier (block/flag/clear) system as the main task pipeline. Blocked tasks can auto-merge with existing tasks via title update.

## Channel 4: Outlook Ingestion

Same schedule as Gmail but uses Microsoft Graph API. `outlook_ingest.py`:

- Same classification pipeline but with **work context** in the Gemini prompt
- Does NOT auto-create people (only looks up existing via ilike)
- Creates pending tasks + drafts for actionable emails
- Uses the same duplicate guard system

## Channel 5: Pulse Staging Sorter

During the scheduled Pulse briefing, the engine fetches ALL pending/staged/synced raw_dumps and runs a batch classification via Gemini:

| Category | Action |
|----------|--------|
| NOTE | Embed → insert into `memories` (source='pulse_note') → mark raw_dump completed |
| NOISE | Mark raw_dump completed (silent discard) |
| TASK | Keep in processing queue → main AI handles as new_task |
| COMPLETION | Keep in processing queue → main AI marks as done |

This means notes sent via Telegram can be "filed" into the memory system during the next Pulse, even if the initial inline processing failed or was skipped.

## Channel 6: Web UI QuickChat

The frontend QuickChat input (on the Home dashboard module) proxies to the same `/api/webhook` endpoint as Telegram. Messages are sent via the `/api/send-message` API route (authenticated via `X-API-Key`) with `intent: "QUICK_CHAT"` metadata.

Processing is identical to Telegram text messages: classification → entity routing → intent dispatch. The same 6-stage pipeline runs, including multimodal dispatch for attached files.

**Difference from Telegram**: No `update_id` deduplication (not needed — API calls are synchronous), and responses return as JSON to the frontend rather than Telegram messages.

## Channel 7: Web UI QuickCommand

The QuickCommand module provides direct typed input for 3 modes without classification:

- **Query**: Sends a `?query` command — hybrid brain interrogation, returns synthesized answer to the dashboard
- **Note**: Creates a note directly (equivalent to `N:` prefix in Telegram)
- **Task**: Creates a task directly (equivalent to classified TASK intent)

Unlike QuickChat, QuickCommand avoids Gemini classification entirely — the user explicitly selects the intent. This is the fastest path for known-intent input from the dashboard.
