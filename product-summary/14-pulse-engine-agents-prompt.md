# 14. The Pulse Engine — Multi-Agent Orchestration & Prompt Engineering

## 5 Parallel AI Agents

During the context-building phase, the engine runs 5 specialized agents in parallel via `asyncio.gather()`. Each agent returns a text block that's injected into the briefing prompt.

### Agent 1: Dependency Agent
**Source**: `core/pulse/graph.py` — `check_task_dependencies()`
Reads `graph_edges` with `DEPENDS_ON` relationships and returns a dependency chain analysis. Helps the briefing AI understand task sequencing.

### Agent 2: Social Graph Optimizer
**Source**: `core/pulse/graph.py` — `analyze_communication_patterns()`
Analyzes communication frequency between people in the knowledge graph. Identifies who talks to whom most often, surfacing collaboration patterns.

### Agent 3: Temporal Pattern Detector
**Source**: `core/pulse/memory.py` — `detect_temporal_patterns()`
Queries memories from the same month/day across ALL previous years using PostgreSQL ilike pattern matching. Returns top 5 "On this day" memories, deduplicated by content.

### Agent 4: Serendipity Engine
**Source**: `core/pulse/memory.py` — `serendipity_engine()`
Uses PostgreSQL Recursive CTEs (`find_serendipity_paths` RPC) to discover hidden 2nd and 3rd degree connections between today's active tasks and the broader knowledge graph. Returns formatted paths showing how tasks connect through intermediate nodes (people, projects, concepts) with their edge relationships and connection weights. Samples up to 30 paths to prevent token bloat.

### Agent 4.5: Graph Centrality (Hub Detection)
**Source**: `core/pulse/graph.py` — `get_graph_centrality_context()`
Executes `get_most_connected_nodes` RPC to rank entities by edge count. Highlights the top 3 most connected people or projects (hubs) in the knowledge graph.

### GRAPH INTELLIGENCE Context Block

Injected into the prompt alongside the 5 agents:
- **`graph_task_context`** — Lists edges connecting people to active tasks (INVOLVES, MANAGES, ASSIGNED_TO). With the backfill connecting all emotional_state nodes to Danny via FEELS edges, the LLM now receives emotional context in the opening synthesis (e.g., "You've logged heavy emotional weight around depression this week...").
- **`dependency_context`** — Flags tasks blocked by uncompleted predecessors (DEPENDS_ON/BLOCKED_BY edges).
- **`social_graph_context`** — Surfaces under-communicated strategic contacts.
- **`serendipity_context`** — Cross-domain connection paths discovered via recursive CTEs.

### Agent 5: Adaptive Briefing Learner
**Source**: `core/pulse/memory.py` — `adaptive_briefing_learner()`
Three meta-learning mechanisms:
1. **Time-of-day effectiveness**: Compares morning vs. evening memory creation rates to suggest adjusting briefing depth
2. **Section density learning**: Detects sparse organization_name sections (<2 tasks) and suggests condensing
3. **Token optimization**: Recommends keeping briefings under 3 bullets per section

## The Research Worker Agent (Background Scheduled)

Not part of the 5 parallel agents (those run inline during Pulse). The Research Worker is a standalone GitHub Actions workflow (`research_worker.yml`, runs 2x daily) that processes the `agent_queue`.

### The Flow

1. A Telegram message classified as `DELEGATE` inserts an entry into `agent_queue` with `status='pending'`
2. On the next research_worker run:
   - Picks up pending queue items
   - For each: Jina AI searches the web for the research topic
   - Search results sent to Gemini for dossier synthesis
   - Dossier saved to `raw_dumps` with `message_type='research'`
   - Queue item marked as completed
3. Danny receives a Telegram notification: "Research complete. Summary in next briefing."
4. Next Pulse briefing includes the research findings in context

**File**: `core/agents/research_agent.py`

### Key Characteristics

- Runs independently of the Pulse cycle — research is async and can complete between briefings
- Jina AI search provides up-to-date web content
- Gemini synthesis produces structured, condensed dossiers (not raw search results)
- Results feed into the next briefing's context, not the current one

## The 250-Line System Prompt

The briefing prompt given to Gemini is one of the most carefully engineered components of the system. It contains 30+ hard constraints organized into these sections:

### Core Identity
The AI is told who it is (a dry, direct operator) and who it's talking to (Danny, operating across multiple domains).

### Strategic Context
The current season context is injected with expiry detection. If expired, the prompt starts with "CRITICAL: Season Context EXPIRED."

### Persona Guidelines
The time-of-day mode determines tone, structure, and attention profile. Each mode has explicit rules about what to include and exclude.

### Data Fidelity Rules (Non-Negotiable)

```
- STRICT DATA FIDELITY FOR BRIEFING: You are STRICTLY FORBIDDEN from listing any task
  in ANY section that does not appear verbatim in the SYSTEM TASKS list.
- Do NOT surface tasks from HINDSIGHT MEMORIES, Canonical Pages, or any other context
  into the briefing output. All context is for intelligence and routing only.
- HINDSIGHT_MEMORIES are for THE COMPASS (Opening Synthesis) ONLY.
- NEVER create tasks from URLs unless explicitly commanded.
- NEVER mark tasks done unless input explicitly matches.
```

### Tone Guards

```
- NEVER use: momentum, focus, gentle, reflection, push, strategic, SITREP, optimal,
  cluster, ready for your review, Operational, Vanguard, Battlefield, Chief of Staff
- Talk like a friend who is also a high-level operator.
- Be direct, simple, human.
```

### Format Rules

```
- Every section header and EVERY task MUST occupy its own individual line.
- Section headers (🚀 Work, 🏠 Home, ⛪ Church) MUST be preceded by two newlines.
- Double-space before headers, single-space after them.
- Max 3 items per section. Append "...and X more in /library or /vault".
- Use actual carriage returns (real newlines) — NOT literal '\n' text characters.
- No numbering, no IDs, no weights, no parentheses in output.
```

### Revenue Critical Bolding

Tasks involving Sales, Pilots, or Payments are marked with `is_revenue_critical: true` and bolded in the briefing using `**task title**`.

## The Write Phase Details

### Batch Task Insert (engine.py:1398-1479)

For each task in `ai_data['new_tasks']`:
```python
task_insert = {
    "title": task_title,
    "project_id": resolved_via_7_stage_cascade,
    "priority": task.get('priority', 'important'),
    "status": "todo",
    "estimated_minutes": task.get('estimated_duration', 15),
    "duration_mins": task.get('estimated_duration', 15),
    "reminder_at": format_rfc3339(task.get('reminder_at')),
    "is_revenue_critical": task.get('is_revenue_critical', False),
    "dedup_key": MD5(title + project_id),
}
```

Then for each inserted task, async background operations:
- `write_graph_edges_for_task()` — creates task node + BELONGS_TO + INVOLVES edges
- `sync_to_google()` — creates Google Tasks entry
- `sync_to_calendar()` — creates Google Calendar event (only if explicit time)
- De-clash: if two tasks conflict, stagger by 15 min

### Project Insert (engine.py:1093)
Only for AI-generated `new_projects`. After insert, graph node is created (or upgraded if a matching node exists with a different type).

### People Insert (engine.py:1161)
Only for AI-generated `new_people`. No graph node created here — relies on backfill.

### Cluster Insert (engine.py:1507-1519)
For AI-generated `new_clusters` (triggered when 3+ items suggest a cohesive new goal). After insert, historical resources are backfilled against the new cluster at ≥0.70 confidence.

### The Architect's Final Repair (engine.py:~1765)

After the AI briefing string is received, it's post-processed:
1. Replace `\n` literal text characters with actual newlines
2. Fix broken section header spacing
3. Strip any remaining IDs or metadata patterns
4. Normalize multiple consecutive newlines
5. Ensure every section starts on its own line

This guarantees formatting consistency even if the AI "whispers" (produces slightly malformed output).

Email/call/WhatsApp pending decision blocks are **not** appended here anymore — they've been moved to a standalone **Decision Pulse** (`process_decision_pulse()`) that runs on every cron trigger without any AI call.
