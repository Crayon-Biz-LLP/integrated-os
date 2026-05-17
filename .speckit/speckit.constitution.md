# Rhodey OS — System Constitution
> Last Updated: May 2026 | Maintained by: Danny (Yashwant Daniel), Crayon

---

## 1. What This System Is

Rhodey OS is a **Persistent Executive Memory and Operational Workflow System** built for a single operator: Danny.

It is NOT:
- A general-purpose chatbot
- A team collaboration tool
- A generic RAG application

It IS:
- An event-driven, multi-layer memory architecture
- A personal OS for capture, classification, task management, email triage, briefings, and long-term strategic intelligence
- A system that must be trustworthy before it is clever

---

## 2. Core Governing Principles

These are non-negotiable. Every piece of code, every feature, every change must comply.

### P1 — Zero Silent Failures
Every exception must be logged. `except: pass` is BANNED.
Every caught error must be written to `system_audit_logs` with:
- `function_name`
- `error_message`
- `raw_input` (truncated to 500 chars)
- `created_at` (IST timezone)

### P2 — Atomic State Transitions
A record must NEVER be marked `completed` before its downstream write is confirmed.

The correct pattern:
```
staged → [embed] → [write to memories] → completed
                ↓ (on failure)
          embedding_failed → (retry queue)
```

Marking `completed` before `memories` insert is FORBIDDEN.

### P3 — Idempotency at Every Insert Point
Every Telegram webhook update carries an `update_id`. It MUST be checked against `processed_updates` before processing. Duplicate processing is a bug, not a feature.

All inserts on `raw_dumps` must check for exact content + source duplication within the last 60 seconds before inserting.

### P4 — Timezone Default is Asia/Kolkata (IST)
All timestamps, reminders, and time-aware logic operate in `Asia/Kolkata` unless explicitly declared otherwise. UTC is for storage only. IST is for display and business logic.

### P5 — Literal Fidelity Over Interpretation
The classifier must mirror Danny's exact words in the `title` field. NEVER infer, rename, or "improve" the phrasing of a task or note. If Danny says "check with Vasanth," the title is "check with Vasanth" — not "Follow up with Vasanth on project deliverables."

### P6 — Dead Letter Before Discard
If a record fails processing after 3 attempts, it goes to `dead_letter_queue` — not deleted, not ignored. Danny reviews and resolves manually.

### P7 — Fail Closed on Auth
If `PULSE_SECRET` cannot be verified, reject the request with 401. Never assume trust. Never log the secret itself.

### P8 — No Hallucinated Actions
The system confirms capture. It does NOT promise to execute. Rhodey never says "I'll ping X" or "I'll handle this." Rhodey says "Logged."

### P9 — Entity Routing is Stealth
The `entity` field (SOLVSTRAT, PERSONAL, QHORD, ASHRAYA, CRAYON, INBOX) is assigned in the JSON — it is NEVER mentioned in the receipt text sent to Danny via Telegram.

### P10 — System Health is Observable
If the pipeline has not processed a record in 60+ minutes during business hours (9am–10pm IST), the Janitor must alert Danny via Telegram.

---

## 3. Data Architecture Principles

### Tables and Their Roles
| Table | Role | Mutable? |
|---|---|---|
| `raw_dumps` | Source of Truth — raw intake from all channels | Status field only |
| `memories` | Embedded notes for semantic retrieval | No (append-only) |
| `tasks` | Active and historical task records | Status, priority, reminderat only |
| `graph_nodes` | Entity registry (people, projects, orgs) | Label and metadata |
| `graph_edges` | Relationships between nodes | Additive |
| `canonical_pages` | AI-synthesized truth pages per entity | Versioned, not overwritten |
| `dead_letter_queue` | Failed records for manual review | Danny resolves |
| `system_audit_logs` | Immutable event log | Never modified, never deleted |
| `email_drafts` | Pending outbound reply drafts | Status only |
| `email_pending_tasks` | Danny's decision queue from email parsing | danny_decision field only |

### Schema Rules
- Every table must have `created_at TIMESTAMPTZ DEFAULT NOW()`
- Every table that changes state must have `updated_at TIMESTAMPTZ`
- Embeddings are `vector(768)` — Gemini `gemini-embedding-2-preview` with `output_dimensionality: 768`
- Soft deletes only — no `DELETE` in application code. Use `is_archived = TRUE`

---

## 4. Classification Rules

### Intent Categories
| Intent | Trigger | Destination |
|---|---|---|
| `TASK` | Implies an outstanding action by Danny | `raw_dumps` → `tasks` via Pulse |
| `NOTE` | Describes something that HAS happened, or a strategic insight | `raw_dumps` → `memories` |
| `QUERY` | Danny asking a question to retrieve past info | `interrogate_brain()` |
| `DELEGATE` | Research, audits, autonomous work | `agent_queue` |
| `NOISE` | OTPs, trivial acks, test messages | Discarded silently (logged in audit) |
| `CLARIFICATION_NEEDED` | Ambiguous input | Clarification loop, saved to raw_dumps |

### Classification Confidence Threshold
- ≥ 0.6: Route to intent handler
- < 0.6: Escalate to clarification

---

## 5. The Operator Contract

Rhodey OS exists to serve ONE operator: Danny.

The system must preserve:
- **Low cognitive load** — Danny should not need to think about system mechanics
- **Low data loss** — Every input Danny sends must be captured, even if processing fails
- **High trust** — Danny must be able to rely on the system's output as accurate

The system must NOT:
- Create duplicate tasks
- Mark work as done before it is confirmed
- Silently drop any input
- Lie about what it logged

---

## 6. Deployment Safety Rules

- No feature is deployed without a migration rollback plan
- No prompt change is made without testing on the last 20 `raw_dumps` records
- No model is changed without updating `EMBEDDING_MODEL` and `CLASSIFICATION_MODEL` constants in both `core/webhook/handler.py` and `core/pulse/engine.py`
- GitHub Actions workflows must have `timeout-minutes` set — no unbounded runs
- All secrets referenced in workflows must exist in the GitHub org secrets vault before the workflow is pushed

---

## 7. What "Done" Means

A feature is DONE when:
1. It handles the happy path correctly
2. It handles failure without data loss
3. It logs to `system_audit_logs`
4. It has been tested with a real Telegram or webhook input
5. It does not break any existing flow confirmed in the last 14 days

