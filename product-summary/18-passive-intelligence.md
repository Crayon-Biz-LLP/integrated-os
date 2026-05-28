# 18. Passive Intelligence — What It Discovers Without Being Told

Integrated-OS has multiple passive intelligence systems that discover patterns, connections, and insights without explicit user commands. These run as part of the Pulse briefing cycle or as standalone scheduled jobs.

## Serendipity Engine

The serendipity engine (`memory.py:259-320`) actively hunts for non-obvious connections across domains. Three discovery layers:

### Layer 1: Cross-Domain Keyword Bridges

Finds words >4 characters that appear in 2+ task titles from different org_tags.

Example:
- SOLVSTRAT task: "Prepare Qhord pricing review"
- PERSONAL note: "Read book on pricing strategy"

The engine detects the keyword "pricing" bridging work and personal domains, and surfaces this as a serendipitous connection.

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

The `detect_drift()` RPC (wrapped in `temporal_lineage.py:179-197`) monitors project update frequency:

```python
def detect_drift(project_name: str, hours_window: int = 48) -> dict:
    result = supabase.rpc("detect_drift", {
        "project_name": project_name,
        "hours_window": hours_window
    }).execute()
```

If a project has been updated 3+ times in 48 hours, the briefing prompt flags it as a potential bottleneck, allowing the AI to call attention to churn or indecision.

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

## Resource Mission Backfill

After new missions are created (either by Pulse AI or Telegram /mission command), the system backfills `mission_id` on all historical unlinked resources. It uses Gemini to classify each resource against mission descriptions, but only assigns at ≥0.80 confidence — conservative by design.
