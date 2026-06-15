# 19. Practices & Spiritual Rhythms

The practices system is one of the most unique features of Integrated-OS. It passively detects recurring personal habits and spiritual disciplines from raw text, tracks their health over time, and surfaces correlations with task completion.

## Passive Practice Detection

### Candidate Sources

Practices are detected exclusively from `raw_dumps` entries (`message_type IN ('task', 'note')`). Memories are **not** used as candidates — all Telegram messages generate both a raw_dump and a memory, so the raw_dumps path covers the same data with better metadata (entity routing tags).

### The Two-Pass Approach

**Pass 1: Embedding Clustering** (`practices.py:168-190`)
Every Pulse run, the system generates embeddings for all candidate entries from `raw_dumps`, then clusters by cosine similarity ≥ 0.75.

**Pass 2: Gemini Batch Verification** (`practices.py:317-348`)
Each cluster is sent to Gemini to determine:
- Is this a genuine recurring personal habit? (NOT work)
- What is a canonical name for it?
- Does it span ≥2 calendar weeks?

### What Gets Excluded

Only entries explicitly tagged `PERSONAL` can become practices. Null/unclassified entities and work entities are blocked:
```python
if not entity or entity.upper() != 'PERSONAL':
    continue
```

Entity routing tags are set at classification time (webhook). The NOTE path now correctly passes the entity from classification into both the raw_dump metadata and the memory metadata, ensuring work notes are properly filtered during practice detection.

A cluster spanning ≥2 weeks is required to confirm it's a recurring pattern.

### Exclusion List

Practices can be dismissed via Telegram (`/drop-<practice>`). Dismissed patterns are stored in `core_config` as `dismissed_practice_variants` and filtered out of future detection runs.

## Practice Lifecycle Management

Every practice node in `graph_nodes` (type='practice') goes through a lifecycle:

### Active
- Health score tracked (0-100, based on frequency vs. baseline)
- Occurrence count incremented
- Trend indicators (up/stable/down)
- Appears in the /practices dashboard

### Dormant (28 days without activity)
```python
if days_since_last >= 28:
    status = 'dormant'
```
Still tracked but flagged as drifting. Appears in a separate "Drifting" section.

### Inactive (84 days without activity)
```python
if days_since_last >= 84:
    status = 'inactive'
    # Compress variants (merge similar variants into canonical name)
```
Removed from the active dashboard. Variants are compacted for potential future reactivation.

## Practice Correlation Analysis

The system measures whether practices correlate with task completion rates (`practices.py:656-753`):

```python
# Requires ≥20 practice occurrences AND ≥50 completed tasks
# Compares completion rate on practice days vs. non-practice days
if completion_on_practice_days > completion_on_non_practice_days:
    correlation = "positive"
```

Example finding: "On days when you exercise (practice), you complete 30% more tasks."

## Rhythm Relationships

Practices can have temporal relationships detected via `build_practice_edges()` (`practices.py:522-654`):

- **PRECEDES**: Practice A typically happens within 4 hours before Practice B
- **FOLLOWED_BY**: Practice B typically within 4 hours after Practice A

These are detected via co-occurrence within a 4-hour sliding window, requiring multiple shared-day overlaps.

## Rhythms Dashboard (Weekly Briefing)

On weekends only, the Pulse generates a rhythms section with:

```python
━ Active (5) ━
Meditation          ████████░░  80%  ✓
Exercise            ██████░░░░  60%  →
━ Drifting (1) ━
Reading             ████░░░░░░  40%  ↓
```

Each practice shows:
- Visual health score bar (10 segments: █ = filled, ░ = empty)
- Numeric percentage
- Trend indicator (✓ stable, → declining, ↑ improving)
- Health score = (actual occurrences / expected occurrences) × 100

## Declaring a Practice via Telegram

Practices can also be explicitly declared:

```
User: "I want to start meditating daily"
System (classifies DECLARE_PRACTICE):
    → Creates graph_node with:
        - type='practice'
        - metadata: declared=true, health_score=100, baseline bootstrap
        - Embedding similarity check (≥0.85) prevents duplicates
```

## Practice → Canonical Page Sync

Active practices with sufficient data get canonical pages created (`sync_practice_canonical_pages()`, `practices.py:755-892`). These are versioned, never-overwrite master pages that the brain synthesis job can enrich over time.
