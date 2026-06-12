# 27. Personal Capture Pipeline

How Rhodey captures Danny's own thoughts — meeting notes, ideas, project updates, voice memos — as distinct from external comms (email, WhatsApp, Teams).

## The Gap

Before this pipeline, the system had excellent external intake but zero internal capture. Danny's observations, meeting outcomes, random ideas, and project status updates lived only in his head. The only way to store them was via the `N:` prefix or NOTE classification, which required conscious intent and had no entity/project linking.

## Architecture

Four phases, built incrementally:

### Phase 1 — Classifier Tuning (`core/webhook/classify.py`)

The NOTE intent classifier was augmented with three specific rules:

- **MEETING NOTES & OBSERVATIONS**: "Vasanth call went well", "sync with Ashraya team was productive" — if it describes an outcome without closing a specific task → NOTE, not COMPLETION.
- **PROJECT UPDATES**: "Qhord timeline is tight", "pricing page still open" — status updates without explicit action → NOTE, not TASK.
- **IDEAS**: "What if Atna is middleware instead of full platform?" — speculative thoughts → NOTE, not TASK.

This means Danny can type naturally throughout the day and observations land as notes without any special syntax.

### Phase 2 — Evening Roundup (`api/index.py` → `/api/roundup`)

A scheduled endpoint that runs at 2PM and 8PM IST (via cron-job.org):

1. Validates `x-pulse-secret` header
2. Queries `memories` for today's notes (memory_type IN note, Journal, relationship_note)
3. If ≥3 notes already captured, silently skips (anti-nag guard)
4. Otherwise sends Telegram: "🌆 Evening roundup — any meeting notes, ideas, or project updates from today?"
5. Danny replies naturally → Phase 1 classifier routes to NOTE → embedded and stored

### Phase 3 — Voice Memo Pipeline (`core/webhook/multimodal.py`)

Audio files (Telegram voice messages, audio recordings) now get:

- **Audio-aware extraction prompt**: "Transcribe this audio message exactly as spoken" instead of the image OCR prompt
- **No `ALT IMAGE:` prefix**: Audio transcripts bypass the prefix so they don't skew classifier routing
- **`extraction_method: voice_memo`**: Explicit metadata tag for downstream analysis
- **Clean NOTE pipeline**: Transcribed voice notes flow through the tuned NOTE classifier

### Phase 4 — `/note` Command (`core/webhook/handler.py`)

A new Telegram command for explicit note capture:

```
/note Vasanth is leaning toward Solvstrat for Q3
```

**Flow:**
1. Strips `/note ` prefix
2. Runs `classify_intent()` normally — extracts entity, project, person metadata
3. Overrides `intent → NOTE`, `confidence → 1.0` (preserves `suggested_project`, `linked_person_id`)
4. Passes overridden classification to `route_by_intent()` → `handle_confident_note()`
5. Bot replies: `🧠` (silent confirmation — "Rhodey has it")

**Empty state handling:**
- Bare `/note` → bot asks "What's on your mind?"
- Sets `WAITING_FOR_NOTE` session flag
- Next message within 5 minutes → auto-prepended with `/note ` and processed
- After 5 minutes → flag silently cleared

## Capture Methods (Ranked by Friction)

| Method | Friction | When to Use |
|--------|----------|-------------|
| Natural speech | Zero | Throughout the day — just type observations naturally |
| `/note <text>` | Low | When you want 🧠 confirmation it landed |
| Evening roundup reply | None | Bot prompts you at 2PM/8PM — reply naturally |
| `N:` / `Note:` prefix | Low | Legacy fallback — bypasses classifier entirely |

## Storage

All paths funnel through `handle_confident_note()` in `core/webhook/dispatch.py:288`:

1. Raw text → `raw_dumps` (message_type='note', status='staged')
2. Gemini embedding → `memories` (memory_type='note', source='webhook')
3. Entity/project metadata preserved from classifier output
4. URLs extracted → saved to `resources`
5. Status updated to 'processed' on success, 'embedding_failed' on error

## Downstream Effects

Notes captured via this pipeline surface in:
- **Brain interrogation** (`dispatch.py:interrogate_brain`) — vector search across notes
- **Pulse context hydration** (`pulse/context.py`) — `match_memories_hybrid` picks up note embeddings
- **Serendipity engine** (`pulse/memory.py:serendipity_engine`) — cross-domain connections via graph
