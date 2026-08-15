# 18. Passive Intelligence — What It Discovers Without Being Told

Integrated-OS has multiple passive intelligence systems that discover patterns, connections, and insights without explicit user commands. These run as part of the Pulse briefing cycle or as standalone scheduled jobs.

## Serendipity Engine

The serendipity engine (`memory.py:259-320`) actively hunts for non-obvious connections across domains. Three discovery layers:

### Layer 1: Graph Multi-Hop Discovery

Seeds `find_serendipity_paths` (PostgreSQL Recursive CTE, depth-3) with the graph nodes of active tasks, detected practice/pattern terms, and people/resource labels. Surfaces unexpected 2nd- and 3rd-degree links between today's tasks and historical projects, people, or resources.

Example: a task's graph node connects to a person who also links to an unrelated resource — the engine surfaces that hidden bridge. (The older "cross-domain keyword bridges across organization_names" description predates the org-tag removal, db/75.)

### Layer 2: People-in-Resources

Searches resource titles and descriptions for person names that aren't explicitly linked to those resources via graph edges.

Example: A resource titled "GTM Strategy Guide" mentions "Sunju's framework" → the engine flags that Sunju is connected to this resource even though no explicit graph edge exists.

### Layer 3: Temporal Serendipity

Notes when resources and memories were created on the same day, suggesting hidden connections.

The findings are injected into the briefing prompt as context, allowing the AI to weave them into the Compass opening.

## Adaptive Briefing Learner

The adaptive briefing learner (`memory.py:322-392`) is a meta-learning system that watches how the user interacts with briefings and adjusts future ones. Three mechanisms:

### Time-of-Day Effectiveness Tracking

Compares memory creation rates in morning briefings vs. evening briefings:
```python
# If morning produces more outputs → suggest longer morning briefings
# If evening is sparse → suggest condensed evening briefings
```

### Section Density Learning

Detects org tag sections with fewer than 2 tasks and suggests condensing them into a single "Everything else" line.

### Token Optimization Tips

If briefings consistently exceed token budgets, the learner suggests reducing section sizes.

This runs after every pulse and stores suggestions in the audit log for review.

## After-Action Report

The after-action report generator (`memory.py:172-212`) runs at night (hour >= 20 or < 4 IST):

1. Queries tasks completed today (status = 'done' with completed_at = today)
2. Queries tasks still open
3. Sends to Gemini: "Produce a dry After-Action Report — 1-2 sentences"
4. Saves the reflection as a `memory_type: 'reflection'` memory with embedding

This means the system reviews its own day every night — loops closed vs. loops open — and stores the insight for future retrieval.

## Temporal Pattern Detection (On This Day)

The temporal pattern detector (`memory.py:214-257`) queries memories from the same month/day across ALL previous years:

```python
supabase.table('memories')
    .select('content, memory_type, created_at')
    .or_(f"created_at::text.ilike.*{today.month:02}-{today.day:02}*")
    .order('created_at', desc=True)
    .limit(10)
    .execute()
```

Results are:
- Deduplicated by content (same memory appearing multiple times filtered)
- Capped at 5 memories
- Injected into the briefing prompt as temporal context

This is the productivity equivalent of Timehop/Facebook Memories — but for your own data.

## Drift Detection

The `detect_drift()` RPC (wrapped in `temporal_lineage.py:11-33`) monitors entity update frequency. Called with `org_name` from the briefing:

```python
def detect_drift(entity_name: str, hours_window: int = 48) -> dict:
    result = supabase.rpc("detect_drift", {
        "project_name": entity_name,  # RPC param name legacy — accepts org name
        "hours_window": hours_window
    }).execute()
```

If an entity has been updated 3+ times in 48 hours, the briefing prompt flags it as a potential bottleneck, allowing the AI to call attention to churn or indecision.

## Stale Task Detection

Tasks untouched for 7+ days are surfaced automatically. The pulse engine sorts them by age and includes the count in the briefing context. Stale tasks are not automatically archived — the AI can suggest review.

## Urgent Task Nag Logic

If an urgent task has been open for more than 48 hours, it's flagged as "stagnant" in the briefing. The AI can suggest re-prioritization or identify blockers.

## Email Pipeline Discovery

The email ingest pipeline doesn't just process emails — it discovers entities:
- People from email senders (blocklist-protected, deduped)
- People from Gemini classification (`linked_person_name`)
- Project names from email content (fuzzy matched against existing projects)
- Relationship notes from FYI emails with `has_memory_value=true`

## Resource Cluster Backfill

After new clusters are created (either by Pulse AI or Telegram /cluster command), the system backfills `cluster_id` on all historical unlinked resources. It uses Gemini to classify each resource against cluster descriptions, but only assigns at ≥0.70 confidence — conservative by design.

## Tier 4: Session Working Memory

The bot maintains per-conversation working memory that persists across messages within a 15-minute window:

### Active Anchor

The system tracks the primary entity being discussed (person or project) in the conversation's `active_anchor`. This is stored on the **`conversation_threads.active_anchor`** column (thread redesign, Aug 5 — not `conversations.metadata`) and scopes:

- **Tactical map traversal**: `hybrid_search_graph()` receives the anchor's graph node ID directly (exact-match lookup), avoiding fuzzy label search that could hit a different node.
- **Proactive signal checks**: *(removed)* — the old `check_proactive_signals()` / `core/pulse/proactive.py` no longer exist; proactive nudges now live in `awaiting_reply` (snooze escalation) and the Sentinel piggybacks.
- **Serendipity engine seeding**: The anchor's graph node is added to `start_node_ids` in `find_serendipity_paths`.

The anchor is resolved via three steps:
1. LLM extracts the primary entity from the query during anaphora resolution
2. The label is matched against `graph_nodes` (exact match first, then `ilike`)
3. If multiple matches, ties are broken by edge count (most connected wins)

The anchor is cleared after 15 minutes of inactivity or when a new `DAILY_BRIEF` intent is detected.

### Proactive Signals (removed)

The old `check_proactive_signals()` / `core/pulse/proactive.py` were removed — no `💡` proactive note is appended to query responses anymore. Proactive behavior now ships through:
- `core/services/awaiting_reply.py` — the snooze/escalation ladder for pending questions
- `core/pulse/sentinel.py` — S1 proactive delegation alerts + the piggyback maintenance jobs
- `core/services/message_voice.py` — proactive copy for composed messages

### Source Selection Heuristics

Three boolean flags gate which of the 14 data sources are fetched per query:
- `is_schedule`: matches calendar/schedule/meeting/week/today language → fetches calendar, tasks, people, tactical map
- `is_comms`: matches email/message/said/told language → fetches emails, WhatsApp, people, tactical map
- `is_action`: matches task/todo/block/status language → fetches tasks, tactical map, serendipity, hindsight

If none match, all 14 sources are fetched. Each source has a `safe_fetch()` wrapper with graceful degradation — one failing source never blocks others.
