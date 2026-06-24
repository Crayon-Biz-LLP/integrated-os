# 17. Canonical Pages & Brain Synthesis

## Canonical Pages (AI-Synthesized Master Pages)

Canonical pages are the system's ground truth for every active project and domain — consolidated AI summaries synthesized from memories, tasks, logs, resources, and raw dumps. They are updated in-place on each synthesis cycle.

### Structure

Stored in the `canonical_pages` table:

| Column | Purpose |
|--------|---------|
| `title` | Entity name (e.g., "Solvstrat", "Ashraya") |
| `content` | AI-synthesized knowledge summary |
| `embedding` | 768-dim vector for semantic search |
| `project_id` | Links to the `projects` table |
| `is_current` | TRUE for active page, FALSE for archived |
| `version` | Incremented on each update |
| `source_count` | How many source fragments were used |
| `last_synth_at` | When it was last synthesized |
| `is_sparse` | Flag if content is <500 chars |

### The 6-Source Accumulation Model

When `brain_synth.py` runs, it gathers fragments from 6 sources for each active project:

1. `match_memories` RPC — semantic memory search (filtered by project name)
2. `tasks` table — active project tasks, scoped by `project_id`
3. `match_logs` RPC — AI-generated log entries (filtered by project name)
4. `match_resources` RPC — resources linked to the entity (filtered by title/content)
5. `match_raw_dumps` RPC — raw message dumps (filtered by project name)
6. `people` table — person entries matching the entity name

**Fragment filtering**: All RPC results are passed through `filter_fragments_by_project()`, which checks each fragment's `metadata.entity` field and content for the project name. Uses **word-level matching** — if ANY significant word (>2 chars) from the project name appears in the entity or content, the fragment passes. This catches memories tagged with a parent org tag (e.g., `entity: "SOLVSTRAT"`) that belong to a child project (e.g., "Armour Cyber" whose name doesn't appear as a contiguous substring in the fragment). Prevents cross-project contamination while avoiding false negatives for multi-word project names.

### Parent Page Synthesis

Five parent domains have special synthesis logic (Solvstrat, Qhord, Ashraya, Personal, Crayon). When a parent page is processed:

- All 6 standard sources are queried for the parent entity name
- Additionally, tasks from all child projects (linked via `parent_project_id`) are gathered and prefixed with `CHILD_TASK/[status]`
- The Gemini prompt uses an **Executive Summary Writer** persona to produce a high-level domain overview

Sub-pages (e.g., client projects under Solvstrat) use a **Knowledge Curator** persona focused strictly on that specific project.

### Fragment Threshold & Auto-Archiving

Every project must meet a minimum fragment threshold of 5 to qualify for a canonical page. Parent pages bypass this threshold (they always get synthesized even if thin).

If an existing page exists for a project that no longer meets the threshold, it is **automatically archived** (`is_current = False`). This keeps the table clean — old, stale pages don't accumulate.

Projects with `organization_name = INBOX` or `NULL` are completely skipped — they never get pages.

### Safety Guards

```python
MIN_OUTPUT_LENGTH = 300  # Won't replace with less than 300 chars
```

The old `MIN_RETENTION_RATIO` guard (which rejected new content shorter than 60% of existing) was removed because properly scoped pages are legitimately shorter than their contaminated predecessors.

### Page Updates (In-Place)

Canonical page updates are **in-place** — the existing row is updated with new content and version increment:

```python
if existing_id:
    old_version = get_current_version()
    supabase.table('canonical_pages').update({
        "content": new_markdown,
        "embedding": new_embedding,
        "version": old_version + 1,
        "updated_at": now_iso,
        "last_synth_at": now_iso,
    }).eq('id', existing_id).execute()
else:
    supabase.table('canonical_pages').insert({...}).execute()
```

This avoids unique constraint conflicts on the `title` column.

## Brain Synthesis (Nightly Job)

### What It Does

`core/skills/brain_synth.py` is a nightly knowledge consolidation job that:
1. Queries all active projects with a recognized `organization_name` (skips INBOX)
2. For each project, gathers fragments from 6 sources with organization_name-scoped filtering
3. For parent pages, also gathers child project tasks
4. Sends fragments + existing page to Gemini for domain-aware synthesis
5. Updates the page in-place (or creates if new)
6. Auto-archives pages for projects that fell below the 5-fragment threshold

### What It Does NOT Do (anymore)

- **No stale page reaper**: Previously it resurrected orphan pages for deactivated projects. Removed.
- **No minimum retention ratio**: Clean, scoped content is always preferred over contaminated bloat.
- **No versioned supersedes_id inserts**: Pages are updated in-place to avoid unique constraint issues.

### Domain-Aware Prompts

The Gemini prompt differs based on page type:

**Parent pages** get an Executive Summary prompt:
```
ROLE: Executive Summary Writer for Danny's OS.
OBJECTIVE: Write a high-level overview of the {organization_name} domain.
DOMAIN SCOPE: This page covers the {organization_name} domain and its sub-projects only.
EXCLUDE: Any content related to other domains.
```

**Sub-pages** get a focused prompt:
```
ROLE: Knowledge Curator for Danny's OS.
OBJECTIVE: Update the Master Page for {entity_name} (under {organization_name}).
PROJECT SCOPE: This page is ONLY for {entity_name} under {organization_name}.
EXCLUDE: Any content about other projects, clients, or domains.
```

### Why It Matters

Without brain synthesis, knowledge is scattered across memories, tasks, raw_dumps, and resources. A memory about "Solvstrat's Q3 pricing" and a resource link about "competitor analysis" and a task "call re: pricing" are all fragments. Brain synthesis weaves them into a coherent master page that future briefings and queries can reference.

### Query Integration

When the user interrogates the brain via `?query`, canonical pages are included in the hybrid search:
```python
canonical_res = supabase.rpc('match_canonical_pages', {
    'query_embedding': embedding,
    'match_count': 3,
    'match_threshold': 0.65
}).execute()
```

This means a query like `?what do I know about Qhord` returns both vector memories AND synthesized canonical knowledge.

## Journal Entity Mapping

When journal entries are processed by `archive_ingest.py`, the `graphify()` function creates explicit relationship edges:

```python
ENTITY_MAPPINGS = {
    "Solvstrat": ["solvstrat"],
    "Crayon": ["crayon"],
    "Sunju": ["sunju"],
    "Jaden": ["jaden", "jeffery"],
    "Church": ["church", "ashraya"],
    "₹30L Debt": ["debt", "30l"],
}
```

For each entity mentioned in the text:
- Journal mentions "Solvstrat" → `Danny --works_at--> Solvstrat` edge
- Journal mentions "Jaden" → `Danny --parent_of--> Jaden` edge
- Journal mentions "Church" → `Danny --belongs_to--> Church` edge
- Both "Sunju" and "Solvstrat" mentioned → `Sunju --connected_via--> Solvstrat` edge

This means the knowledge graph grows richer with every journal entry, mapping not just Danny's tasks but his relationships, struggles, and communities.
