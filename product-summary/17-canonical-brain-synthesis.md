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
| `organization_id` | FK to `graph_nodes.id` (org-type node — the `organizations` table was dropped in db/75) |
| `is_current` | TRUE for active page, FALSE for archived |
| `version` | Incremented on each update |
| `source_count` | How many source fragments were used |
| `last_synth_at` | When it was last synthesized |
| `is_sparse` | Flag if content is <500 chars |

### The Multi-Source Accumulation Model

When `brain_synth_v2.py` runs, entities are enumerated from `graph_nodes` where `type='organization'` and `is_current=True`, and fragments are gathered per entity:

1. `memories` table — direct query scoped by `organization_id` (entity id)
2. `tasks` table — active tasks, scoped by `organization_id`
3. `match_resources` RPC — resources linked to the entity
4. `match_raw_dumps` RPC — raw message dumps
5. `match_emails_hybrid` RPC — email entries
6. `match_whatsapp_hybrid` RPC — WhatsApp entries

(The legacy `match_logs` RPC was **dropped** by db/101; the `match_canonical_pages` RPC was also dropped — canonical pages are now read directly from the table.)

**Fragment filtering**: All RPC results are passed through `filter_fragments_by_org_strict()`, which checks each fragment's `metadata.entity` field and content for the org name. Uses **AND word-level matching** — ALL significant words (>2 chars) from the org name must appear in the entity or content. This catches memories tagged with a parent org tag (e.g., `entity: "SOLVSTRAT"`) that belong to a child project (e.g., "Armour Cyber" whose name doesn't appear as a contiguous substring in the fragment). Prevents cross-org contamination while avoiding false negatives for multi-word org names.

### Parent Page Synthesis

Five parent domains have special synthesis logic (Solvstrat, Qhord, Ashraya, Personal, Crayon). When a parent page is processed:

- All 6 standard sources are queried for the parent entity name
- Child tasks (under the same `organization_id`) are gathered and prefixed with `CHILD_TASK/[status]`
- The Gemini prompt uses an **Executive Summary Writer** persona to produce a high-level domain overview

Sub-pages (e.g., client projects under Solvstrat) use a **Knowledge Curator** persona focused strictly on that specific entity.

### Fragment Threshold & Auto-Archiving

Every org-level entity must meet a minimum fragment threshold of 5 to qualify for a canonical page. Parent domains bypass this threshold (they always get synthesized even if thin).

If an existing page exists for an entity that no longer meets the threshold, it is **automatically archived** (`is_current = False`). This keeps the table clean — old, stale pages don't accumulate.

Entities with `organization_name = INBOX` or `NULL` are completely skipped — they never get pages.

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

`core/skills/brain_synth_v2.py` is a nightly knowledge consolidation job that:
1. Queries all active organizations with a recognized `organization_name` (skips INBOX)
2. For each entity, gathers fragments from 6 sources with organization_name-scoped filtering
3. For parent pages, also gathers child tasks
4. Sends fragments + existing page to Gemini for domain-aware synthesis
5. Updates the page in-place (or creates if new)
6. Auto-archives pages for entities that fell below the 5-fragment threshold

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
EXCLUDE: Any content about other domains, clients, or projects.
```

### Why It Matters

Without brain synthesis, knowledge is scattered across memories, tasks, raw_dumps, and resources. A memory about "Solvstrat's Q3 pricing" and a resource link about "competitor analysis" and a task "call re: pricing" are all fragments. Brain synthesis weaves them into a coherent master page that future briefings and queries can reference.

### Query Integration

When the user interrogates the brain via `?query`, canonical pages are included via a **direct table read** (`core/webhook/dispatch.py`):
```python
res = supabase.table('canonical_pages').select('title, content, last_synth_at')\
    .eq('is_current', True).ilike('title', f"%{search_val}%").limit(1).execute()
```

(`match_canonical_pages` was dropped by db/101 — direct reads replaced it.) This means a query like `?what do I know about Qhord` returns both vector memories AND synthesized canonical knowledge.

## Journal Entity Mapping

When journal entries are processed by `archive_ingest.py`, the `graphify()` function creates explicit relationship edges. The entity→keyword mapping is read from the tenant's `core_config` row (`get_entity_mappings()`, per-tenant; M6 de-personalization — no hardcoded entities in code, `DEFAULT_ENTITY_MAPPINGS` is only the tenant #1 legacy fallback).

For each entity mentioned in the text:
- Journal mentions "Solvstrat" → `Danny --works_at--> Solvstrat` edge
- Journal mentions "Jaden" → `Danny --parent_of--> Jaden` edge
- Journal mentions "Church" → `Danny --belongs_to--> Church` edge
- Both "Sunju" and "Solvstrat" mentioned → `Sunju --connected_via--> Solvstrat` edge

This means the knowledge graph grows richer with every journal entry, mapping not just Danny's tasks but his relationships, struggles, and communities.
