# 63. Comprehensive User Testing Plan — Integrated-OS (Rhodey)

**Date**: July 22, 2026  
**Scope**: End-to-end manual user testing across all 6 layers of the system  
**Target**: Validate every user-facing flow, edge case, and integration point  

---

## How to Use This Plan

This is a **manual testing playbook**. Each section contains:
- **Prerequisites** — What must be true before testing
- **Test scenarios** with step-by-step instructions
- **Expected outcomes** — What you should see in Telegram, Web UI, and the database
- **Pass/Fail criteria**

Run scenarios in order within each layer. Earlier layers (Ingest → Process) must pass before testing higher layers (Intelligence → Presentation). Use the **Verification Queries** section at the end for DB-level checks.

---

## LAYER 1: INGESTION (Capture & Intake)

### 1.1 Telegram Text Capture

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| T1 | **Simple task creation** | Send: "Prepare Q3 pricing proposal — due Friday 3pm" | 1. Raw_dump created with `status='classified'` or `pending`<br>2. Task appears in DB within 5s<br>3. Calendar event created for Friday 3pm IST<br>4. Google Tasks entry created<br>5. Telegram receipt: "Task logged." or similar | ☐ |
| T2 | **Simple note (FYI)** | Send: "N: The Qhord GTM should emphasize API-first approach" | 1. Memory created (memory_type='note')<br>2. No task created<br>3. Telegram: "Noted." | ☐ |
| T3 | **Query (brain interrogation)** | Send: "What do I know about Qhord GTM?" | 1. Classified as QUERY<br>2. Response includes synthesized answer from memories + tasks + graph<br>3. Sources cited (if applicable)<br>4. Response rendered in Telegram | ☐ |
| T4 | **Task completion** | Send: "Done with the Qhord pricing review" | 1. Classified as COMPLETION<br>2. Matching active task found and closed<br>3. Calendar event deleted (if existed)<br>4. Google Tasks marked complete<br>5. Outcome memory created<br>6. Telegram: confirmation with task name | ☐ |
| T5 | **Task completion by ID** | Send: "Close task 42" or "Mark 42 done" | 1. Task ID 42 closed<br>2. Outcome memory created<br>3. Telegram confirmation | ☐ |
| T6 | **URL quarantine** | Send: "https://example.com/pricing-guide" | 1. Resource created in `resources` table<br>2. NO task created<br>3. NO memory created<br>4. NO graph entity extraction on URL<br>5. Telegram: confirmation resource was logged | ☐ |
| T7 | **URL in context** | Send: "Check this pricing doc https://example.com/pricing-guide and tell me if we're competitive" | 1. URL quarantined to resources<br>2. Rest of text processed as QUERY<br>3. Llm response addresses the question<br>4. Resource linked if entities match | ☐ |
| T8 | **Note without N: prefix** | Send: "Reminder — Sunju's birthday is next week" | 1. Classifier detects NOTE intent<br>2. Memory created with content<br>3. Telegram: "Noted." or similar | ☐ |
| T9 | **Clarification request** | Send ambiguous: "Follow up with him" (no prior context) | 1. Classifier detects CLARIFICATION_NEEDED<br>2. Response asks "Who should I follow up with?"<br>3. No task created until clarification resolved | ☐ |
| T10 | **Multi-intent message** | Send: "Not needed. Just close all open tasks related to Amita and FC Madras." | 1. WORKFLOW: batch confirm closes tasks<br>2. If ancillary text remains, it's re-classified<br>3. Secondary actions processed with ≥0.5 confidence<br>4. All matching tasks closed<br>5. Summary: "Closed N tasks related to Amita and FC Madras" | ☐ |

### 1.2 Telegram Voice Note Capture

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| V1 | **Voice → task** | Record voice: "Schedule a meeting with Vasanth for Tuesday 11am" | 1. Audio transcribed via Gemini<br>2. Text classified as TASK<br>3. Task created with time context<br>4. Calendar event for Tuesday 11am<br>5. Telegram receipt | ☐ |
| V2 | **Voice → note** | Record voice: "Idea: What if we bundle API access with consulting?" | 1. Audio transcribed<br>2. Classified as NOTE<br>3. Memory created<br>4. Telegram: "Noted." | ☐ |
| V3 | **Voice → query** | Record voice: "What's the status on Qhord?" | 1. Audio transcribed<br>2. Classified as QUERY<br>3. Synthesized answer returned | ☐ |

### 1.3 Document Capture

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| D1 | **PDF text extraction** | Send a PDF with meeting notes (text-based, not scanned) | 1. PyMuPDF extracts verbatim text<br>2. Document classified as NOTE<br>3. Full text preserved (not LLM-summarized)<br>4. Memory created with full text | ☐ |
| D2 | **PDF scanned/image** | Send a scanned PDF (image-based) | 1. Falls back to Gemini vision extraction<br>2. Document classified<br>3. Content preserved | ☐ |
| D3 | **DOCX extraction** | Send a .docx file | 1. python-docx extracts text<br>2. Classified and memory created | ☐ |
| D4 | **XLSX extraction** | Send an .xlsx file | 1. openpyxl extracts text<br>2. Classified and memory created | ☐ |
| D5 | **PPTX extraction** | Send a .pptx file | 1. python-pptx extracts text<br>2. Classified and memory created | ☐ |
| D6 | **Image OCR** | Send a photo of text (whiteboard, document) | 1. Gemini multimodal extracts text<br>2. Classified accordingly | ☐ |

### 1.4 Web UI QuickChat

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| W1 | **QuickChat task** | Dashboard → QuickChat → type "Review contract by Friday" → send | 1. Task created in DB<br>2. Google Calendar event for Friday<br>3. Google Tasks entry<br>4. Response shown in chat | ☐ |
| W2 | **QuickChat query** | Dashboard → QuickChat → type "What do I know about Equisoft?" → send | 1. Classified as QUERY<br>2. Synthesized answer returned in chat | ☐ |
| W3 | **QuickCommand task** | QuickCommand → Task mode → type "Prepare board deck" → send | 1. Task created<br>2. No LLM classification (intent pre-specified)<br>3. Fastest path | ☐ |
| W4 | **QuickCommand note** | QuickCommand → Note mode → type "Key insight from today's standup" → send | 1. Memory created<br>2. No LLM classification overhead | ☐ |
| W5 | **QuickCommand query** | QuickCommand → Query mode → type "?what's my schedule today" → send | 1. Query routed to interrogate_brain<br>2. Answer returned | ☐ |

### 1.5 Email Ingestion

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| E1 | **Actionable email → pending task** | Send yourself an email: "Can you review the Q3 engagement letter?" | Wait for next email_ingest cycle | 1. Email fetched via Gmail API<br>2. Classified as "actionable"<br>3. `messages` row created with `danny_decision=NULL`<br>4. Decision Pulse shows: "📨 [eN] Review Q3 engagement letter"<br>5. Pending task visible in Web UI Email tab | ☐ |
| E2 | **Approve email task → task created** | Reply to Decision Pulse: "eN yes" | 1. Task created in DB<br>2. Calendar event (if time context)<br>3. Google Tasks sync<br>4. `danny_decision='approved'` | ☐ |
| E3 | **Reject email task** | Reply: "eN no" | 1. `danny_decision='rejected'`<br>2. No task created<br>3. No follow-up | ☐ |
| E4 | **FYI email → person link** | Receive email from known contact with FYI content | 1. Message classified as "fyi"<br>2. Sender linked to `people` table<br>3. No task created<br>4. Shown in next briefing as FYI | ☐ |
| E5 | **Outlook email → pending task** | (Same as E1 but via Outlook) | 1. Fetched via Microsoft Graph API<br>2. Work-context prompt used for classification<br>3. Same flow as E1 | ☐ |

---

## LAYER 2: PROCESSING (Task & Note Lifecycle)

### 2.1 Task Lifecycle

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| P1 | **Create task with project** | Send: "Review pricing for Solvstrat client — due tomorrow 2pm" | 1. Project resolved via 7-stage cascade → "Solvstrat"<br>2. Task created with `project_id` and `organization_id`<br>3. Task node created in `graph_nodes` (type='task')<br>4. BELONGS_TO edge to Solvstrat project node<br>5. Google Calendar: Friday 2pm with `⚡ ACTION:` prefix<br>6. Google Tasks entry with title | ☐ |
| P2 | **Create task with org (no project)** | Send: "Handle the Ashraya compliance filing — due next Monday" | 1. Project may not exist → signal created or auto-created<br>2. Organization resolved to Ashraya<br>3. Task created with `organization_id`<br>4. Proper routing in Pulse briefings | ☐ |
| P3 | **Close task → Google sync cleanup** | Close a task that has both calendar event and Google Task | 1. `status='done'`<br>2. `completed_at` set<br>3. Calendar event deleted from Google<br>4. Google Tasks marked complete<br>5. Outcome memory created<br>6. Versioned archive row created | ☐ |
| P4 | **Close task (no calendar)** | Close a task without time context | 1. Status updated to 'done'<br>2. No calendar deletion needed<br>3. Google Tasks marked complete<br>4. Outcome memory created<br>5. Version archived | ☐ |
| P5 | **Update task deadline** | Send: "Push the Solvstrat pricing review to next Tuesday" | 1. Task deadline updated<br>2. Calendar event rescheduled (new event created, old deleted)<br>3. Versioned archive row for old state<br>4. Telegram confirmation | ☐ |
| P6 | **Update task priority** | Send: "Make the Ashraya filing urgent" | 1. Priority changed to 'urgent'<br>2. Calendar prefix updates to `🔥 CRITICAL:`<br>3. Version archived | ☐ |
| P7 | **Cancel vs. done (recurring)** | For a recurring task, send "cancel it" | 1. `status='cancelled'`<br>2. Entire Google Calendar series deleted<br>3. Series ends<br>→ vs. "done it": only current instance skipped, series continues | ☐ |
| P8 | **Recurring task creation** | Send: "Team standup every weekday at 9:30am" | 1. Task created with `recurrence` field<br>2. RRULE created and pushed to Google Calendar<br>3. Recurring series visible in dashboard | ☐ |
| P9 | **Recurring skip instance** | Send: "Skip this week's standup" | 1. Next instance removed from calendar<br>2. Task stays 'todo'<br>3. Series continues | ☐ |
| P10 | **Recurring UNTIL boundary** | Create a recurring task with UNTIL date in the past, then mark done | 1. System detects "No upcoming instances found"<br>2. Task permanently closed as 'done'<br>3. No infinite re-open loop | ☐ |
| P11 | **Semantic dedup prevention** | Send a task that already exists with same meaning but different wording | 1. `check_duplicate()` finds match<br>2. No duplicate task created<br>3. Telegram receipt still shows success (stealth)<br>4. If flagged as overlap, clarification: "Update existing?" | ☐ |
| P12 | **Task assignment to person** | Send: "Ask Sunju to review the Qhord pricing" | 1. Entity extraction finds "Sunju"<br>2. Person resolved in `people` table<br>3. Task created with person context<br>4. INVOLVES edge to Sunju in graph<br>5. Briefing highlights as commitment (direction='outbound') | ☐ |

### 2.2 Note Lifecycle

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| N1 | **Create note → memory** | Send: "N: Idea for Qhord — tiered pricing with API access" | 1. Memory created with full text<br>2. Embedding generated<br>3. Entity extraction triggers → pending edges created<br>4. Graph nodes created if new entities found<br>5. Retrievable via brain interrogation later | ☐ |
| N2 | **Note with expiry** | Send: "N: Today's parking spot is level 3" | 1. Memory created with `expires_at` (end of day)<br>2. After expiry, excluded from retrieval results<br>3. Eventually archived or deleted | ☐ |
| N3 | **Email → note memory** | Approve an FYI email as note-worthy | 1. Memory created from email body<br>2. Sender linked as person reference<br>3. Entity extraction on content | ☐ |

### 2.3 Enrichment Queue

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| Q1 | **Enrichment survives cold start** | Create a task → immediately force Vercel cold restart | 1. Task created in DB (inline, survives restart)<br>2. Enrichment job in `pending_enrichment_jobs` (survives restart)<br>3. Next sentinel piggyback picks it up<br>4. Graph edges created, entities extracted | ☐ |
| Q2 | **Enrichment retry on failure** | Cause an enrichment to fail (e.g., temporary Gemini API error) | 1. `pending_enrichment_jobs` shows `retry_count` incremented<br>2. Job retried up to 3 times<br>3. After 3 failures → moved to dead letter | ☐ |

---

## LAYER 3: INTELLIGENCE (Knowledge & Retrieval)

### 3.1 Knowledge Graph

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| G1 | **Entity extraction on creation** | Create a task mentioning "Armour Cyber" and "Equisoft" | 1. Entity_extractor fires<br>2. Graph nodes found/created for both entities<br>3. Pending edges created (e.g., WORKS_AT, BELONGS_TO)<br>4. Edges visible in Decision Pulse for approval | ☐ |
| G2 | **Approve graph edge via Telegram** | See pending edge "peN" in Decision Pulse → reply "peN yes" | 1. Edge moves from `pending_graph_edges` to `graph_edges`<br>2. Both nodes linked<br>3. Decision logged in `decisions` table<br>4. Edge visible in graph UI | ☐ |
| G3 | **Reject graph edge** | Reply "peN no" | 1. Edge archived or rejected<br>2. No graph_edges entry created<br>3. Decision logged | ☐ |
| G4 | **Approve graph node (person)** | See pending node "gN" in Decision Pulse → reply "gN yes" | 1. `pending_nodes` approved<br>2. `people` row created<br>3. `graph_nodes` entry created (type='person')<br>4. Back-links established | ☐ |
| G5 | **Reject graph node** | Reply "gN no" | 1. Node rejected, no graph entry created<br>2. `pending_nodes` marked rejected | ☐ |
| G6 | **NLP correction on node type** | For pending node "gN → Marcus" that's classified as person but is an org: reply "gN is an organization" | 1. Node type corrected in `pending_nodes`<br>2. Updates reflected in Decision Pulse<br>3. Correct type on approval<br>4. `graph_type_overrides` recorded | ☐ |
| G7 | **Merge proposals** | Two graph nodes for same entity (e.g., "Sunju" and "Sunju Rajan") | 1. Merge proposal appears in `merge_proposals`<br>2. Decision Pulse shows merge option<br>3. Approval merges nodes, consolidates edges<br>4. Rejection keeps both with status tracked | ☐ |
| G8 | **Graph traversal in query** | Ask: "What's the connection between Equisoft and Armour Cyber?" | 1. Graph query resolves both entities<br>2. Traverses edges between them<br>3. Returns synthesized answer: project relationship, people, work items | ☐ |

### 3.2 Associative Retrieval

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| R1 | **Memory retrieval via query** | Ask: "What did I learn about pricing?" | 1. `associative_retrieve()` runs 7-signal ranking<br>2. Semantic search finds pricing memories<br>3. PPR traversal finds related entities<br>4. Recency/importance signals rank results<br>5. Combined context sent to LLM for synthesis | ☐ |
| R2 | **Cross-entity discovery** | Ask: "What's happening with Sunju?" | 1. Entity anchored to "Sunju"<br>2. Hybrid search: vector + graph PPR + canonical pages<br>3. Returns tasks mentioning Sunju + related memories + canonical page<br>4. Gemini synthesizes complete picture | ☐ |
| R3 | **Temporal retrieval** | Ask: "What happened last week?" | 1. Recency-weighted retrieval<br>2. Events from last 7 days prioritized<br>3. Answer with time-bounded context | ☐ |

### 3.3 Context Registry

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| C1 | **Entity-grounded context (PRE_FLIGHT)** | Ask about "Shifrah" without prior context | 1. Context registry runs PRE_FLIGHT strategy<br>2. Entities resolved: finds Shifrah in graph_nodes<br>3. Semantic retrieval fetches relevant memories<br>4. Anchored context injected into prompt<br>5. Response is accurate — no hallucination | ☐ |
| C2 | **Neutral context penalty** | Ask vague question with no entity anchor | 1. No entity match found<br>2. `semantic_requires_anchor` fails<br>3. Neutral context penalty (0.5x) applied<br>4. No semantic retrieval without anchor<br>5. Response is cautious/limited | ☐ |
| C3 | **Hard gate rejection** | Ask about entity that needs more context | 1. Hard gate triggered<br>2. No context passed to LLM<br>3. Response: "I don't have enough context about that." | ☐ |

---

## LAYER 4: PRESENTATION (Pulse Engine & Automation)

### 4.1 Pulse Briefing

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| B1 | **Morning Pulse delivery** | Wait for 7:30 AM IST weekday Pulse | 1. Briefing sent to Telegram<br>2. Sections: 🔴 Urgent, 🚀 Work, 🏠 Home, ✅ Done<br>3. Tasks filtered by 2-day horizon, 14-day creation window<br>4. Revenue-critical tasks highlighted<br>5. Decision Pulse sent as separate message (no AI)<br>6. Compass opening with hindsight context | ☐ |
| B2 | **Pulse on weekend** | Wait for weekend Pulse (8 AM IST) | 1. Weekend-appropriate tone<br>2. Personal/home tasks prioritized<br>3. Work tasks de-emphasized<br>4. Different cadence than weekday | ☐ |
| B3 | **Pulse with stale hindsight** | No journal entries in last 24h → next Pulse | 1. "The signal is quiet on the reflection front" message<br>2. Briefing still runs with tactical context<br>3. No hallucination of journal content | ☐ |

### 4.2 Decision Pulse

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| D1 | **Pending decisions listed** | Have pending email tasks, graph edges, or nodes | 1. Decision Pulse shows all pending items<br>2. Each item has shortcode (eN, gN, peN)<br>3. Approve/reject with inline keyboard<br>4. No AI involved (pure DB query)<br>5. Delivered in ~2 seconds | ☐ |
| D2 | **Batch approve all** | Send "yes" to Decision Pulse after multi-item | 1. All pending items approved<br>2. Tasks created, edges written, nodes approved<br>3. Summary: "Approved N items" | ☐ |
| D3 | **Undo within 30 min** | Approve an edge → immediately type "/undo" | 1. Decision found in `decisions` table<br>2. Action reversed<br>3. Edge back to pending state<br>4. Telegram: "Undone." | ☐ |
| D4 | **Undo beyond 30 min** | Approve an edge → wait 31 min → "/undo" | 1. No recent decision found<br>2. "Nothing to undo." message | ☐ |

### 4.3 Sentinel (Meeting Alarms)

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| S1 | **Meeting nudge** | Have a calendar event starting in 15 min | 1. Sentinel fires → Telegram nudge<br>2. Event name and time displayed<br>3. Related tasks surfaced if available<br>4. Prep context from memories if applicable | ☐ |
| S2 | **Multiple events in lookahead window** | Have 3 events in the next 60 min | 1. All 3 events listed in nudge<br>2. Ordered by start time<br>3. No event missed | ☐ |
| S3 | **Piggyback maintenance** | Verify that sentinel piggybacks run every ~5 min | 1. Enrichment queue processed<br>2. Index jobs processed<br>3. Workflow expiry checked<br>4. Auto-archive of stale threads<br>5. No errors in audit logs | ☐ |

### 4.4 Health Monitor

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| H1 | **Healthy state** | Check /api/health endpoint | 1. DLQ count returned<br>2. Error log count returned<br>3. LLM degradation status<br>4. Pipeline health status | ☐ |
| H2 | **DLQ items present** | Manually trigger a failure | 1. Health monitor reports DLQ items<br>2. Items surfaced in Health dashboard<br>3. Retry attempted on next cycle | ☐ |

---

## LAYER 5: SURFACE (Telegram, Web UI, Flutter)

### 5.1 Telegram Interactions

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| TE1 | **/why command** | After any bot response, type "/why" | 1. Decision chain shown<br>2. Classification stage: intent, confidence<br>3. Routing stage: handler name<br>4. Context registry: items kept/excluded with reason codes<br>5. Retrieval: sources consulted<br>6. Human-readable format | ☐ |
| TE2 | **Conversational follow-up** | Send "What about Equisoft?" after a Qhord discussion | 1. Thread resolution: finds active thread<br>2. Entity resolution: "Equisoft" via graph<br>3. Active anchor updates<br>4. Response maintains thread context<br>5. No prior message repeats | ☐ |
| TE3 | **Cross-thread awareness** | Discuss "pricing" in one thread, then in another thread mention "the pricing model I was looking at" | 1. Awareness layer scans recent threads<br>2. Cross-reference detected<br>3. Injected as ACTIVE CONVERSATION CONTEXT<br>4. LLM has context from both threads | ☐ |
| TE4 | **Workflow resume** | Bot asks a question → user replies | 1. Open workflow found by chat_id<br>2. Reply matched to open question<br>3. Workflow continues without re-classifying<br>4. After resolution, workflow status = 'resolved' | ☐ |
| TE5 | **Unrelated reply during workflow** | Bot asks "Confirm this task?" → user replies "Did you see the email?" | 1. Unrelated note detected<br>2. Workflow preserved (not cancelled)<br>3. Response handles the note<br>4. Workflow still active for later | ☐ |
| TE6 | **Streaming response** | Send a QUERY that requires thinking | 1. Response starts appearing immediately (streaming)<br>2. EditMessageText updates progressively<br>3. Full response at end | ☐ |
| TE7 | **Timeout handling** | Send a request that would take >55s | 1. "Still thinking..." message sent<br>2. Processing continues in background<br>3. Result delivered when ready (or next pulse)<br>4. No 502/504 error to user | ☐ |

### 5.2 Web UI Dashboard

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| U1 | **Dashboard loads** | Open dashboard URL | 1. Stats cards render: Open tasks, Due today, Overdue, Pending emails<br>2. WhatToDoNow section shows prioritized items<br>3. QuickChat available<br>4. Pulse Briefings section shows last 3<br>5. Recent tasks list | ☐ |
| U2 | **Task table** | Navigate to Tasks tab | 1. All open tasks shown in table<br>2. Sort by priority, due date, project<br>3. Filter by status/project/org<br>4. Detail sheet on click | ☐ |
| U3 | **Task completion via UI** | Click "Done" on a task | 1. PATCH /api/tasks/{id}/status called<br>2. Calendar event deleted<br>3. Google Tasks synced<br>4. Versioned update created<br>5. Outcome memory written<br>6. Dashboard refreshes | ☐ |
| U4 | **Email view** | Navigate to Emails tab | 1. Inbox table with filter toggles (actionable, drafts, sent, all)<br>2. Pending tasks with approve/reject<br>3. Draft list with send capability<br>4. Email detail view | ☐ |
| U5 | **Calendar view** | Navigate to Calendar tab | 1. Month/Week/Day/Agenda views toggle<br>2. Google + Outlook events unified<br>3. Events clickable for detail<br>4. Proper IST timezone display | ☐ |
| U6 | **Knowledge Graph view** | Navigate to Graph tab | 1. Split-pane: Episode Stream (left) + NeuralDisc 3D (right)<br>2. Node click shows detail flyout<br>3. Search/filter available<br>4. Zoom/pan works smoothly | ☐ |
| U7 | **Decision Pulse in UI** | Navigate to Decisions tab | 1. Pending edges listed with approve/edit/reject buttons<br>2. Badge count on tab<br>3. Approve/reject updates graph in real-time | ☐ |
| U8 | **People view** | Navigate to People tab | 1. Grid of people with strategic weight<br>2. Role, source, org info shown<br>3. Click shows linked tasks/projects | ☐ |
| U9 | **Resources view** | Navigate to Resources tab | 1. Library grid with cluster grouping<br>2. Grid/list toggle<br>3. Dismiss button works (sets dismissed_at)<br>4. Dismissed resources hidden from queries | ☐ |
| U10 | **Health dashboard** | Navigate to Health tab | 1. Pipeline health status<br>2. DLQ count and items<br>3. Error logs viewable<br>4. Memory stats | ☐ |

### 5.3 Flutter Mobile App

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| F1 | **App loads** | Open Rhodey app | 1. Horizon/Traces home screen renders<br>2. Card-based feed from /api/briefing<br>3. Search bar for tasks/conversations/traces<br>4. Warm stone palette design | ☐ |
| F2 | **Push notification** | Trigger a Telegram message from bot | 1. FCM push notification received<br>2. Notification triggers briefing fetch<br>3. App updates with latest response | ☐ |
| F3 | **Voice input** | Tap voice mic button | 1. Audio recording starts<br>2. TTS for Rhodey responses works<br>3. Voice processed same as Telegram voice | ☐ |
| F4 | **Conversation view** | Open a conversation thread | 1. Chat history displayed<br>2. Send message works<br>3. Responses rendered correctly | ☐ |

---

## LAYER 6: INFRASTRUCTURE (Cross-Cutting)

### 6.1 Google Calendar Integration

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| GC1 | **Create → event sync** | Create task with time → check Google Calendar | 1. Event appears in Google Calendar<br>2. Correct title with priority prefix `⚡ ACTION:`<br>3. Correct time (IST)<br>4. Description: "Rhodey created this for you." | ☐ |
| GC2 | **Delete task → event deleted** | Close a task that had a calendar event | 1. Google Calendar event deleted<br>2. DB `google_event_id` nulled<br>3. No orphan events | ☐ |
| GC3 | **External deletion recovery** | Manually delete an event in Google Calendar → check on next sync | 1. 404 error caught during sync<br>2. DB `google_event_id` nulled<br>3. Fresh event re-provisioned on next task update | ☐ |
| GC4 | **Recurring series** | Create recurring task → check Google Calendar | 1. Recurring event series created<br>2. RRULE correctly applied<br>3. Skip instance removes single occurrence<br>4. Cancel series ends the entire series | ☐ |
| GC5 | **Priority prefix update** | Change task priority from normal to urgent | 1. Calendar event title updates to `🔥 CRITICAL:`<br>2. Old prefix stripped before new one applied<br>3. No prefix stacking | ☐ |

### 6.2 Google Tasks Integration

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| GT1 | **Create → task sync** | Create any task → check Google Tasks | 1. Task appears in Google Tasks<br>2. Title matches<br>3. Due date correct (IST) | ☐ |
| GT2 | **Complete → Google Tasks** | Complete a task locally | 1. Google Tasks marked complete<br>2. Status synced back on next poll | ☐ |
| GT3 | **External complete → sync back** | Mark a task done in Google Tasks directly | 1. Next Pulse or sync cycle detects change<br>2. `create_versioned_task()` creates done archive<br>3. Local DB updated | ☐ |

### 6.3 Dead Letter Queue

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| DL1 | **Failed enrichment → DLQ** | Force enrichment failure (bad data) | 1. After 3 retries → job moved to DLQ<br>2. DLQ entry logged in `failed_queue`<br>3. Health monitor reports DLQ count<br>4. DLQ consumer can retry on next cycle | ☐ |
| DL2 | **DLQ consumer retry** | Fix the underlying issue → DLQ consumer runs | 1. DLQ item picked up<br>2. Processed with exponential backoff<br>3. If successful → removed from DLQ<br>4. If fails again → escalated | ☐ |

### 6.4 State Machine Guards

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| SM1 | **Valid transition** | Change task from 'todo' → 'done' | 1. `guard_is_valid_transition('task', 'todo', 'done')` returns True<br>2. Transition proceeds<br>3. State updated | ☐ |
| SM2 | **Invalid transition** | Change task from 'done' → 'todo' | 1. `guard_is_valid_transition('task', 'done', 'todo')` returns False<br>2. Transition blocked<br>3. Audit log: invalid transition attempt | ☐ |
| SM3 | **All 16 table transitions** | Spot-check a few other tables (raw_dumps, memories, messages) | 1. All valid transitions enumerated in `state_machines.py`<br>2. Invalid transitions properly blocked<br>3. No undefined states reachable | ☐ |

### 6.5 Temporal Lineage (Versioning)

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| TL1 | **Task versioning on update** | Update a task's title 3 times | 1. 3 archive rows created (`is_current=false`)<br>2. Latest row has `version=4, supersedes_id=<prev>`<br>3. Trigger fires on content changes (title, status, project, priority, deadline, reminder_at)<br>4. No versioning on sync fields (google_event_id, google_task_id) | ☐ |
| TL2 | **Time-travel** | Query a task's version history | 1. All versions retrievable<br>2. Each version has correct state at that time<br>3. `supersedes_id` chain intact | ☐ |

### 6.6 Auth & Security

| ID | Scenario | Steps | Expected Outcome | Pass/Fail |
|----|----------|-------|------------------|-----------|
| A1 | **API key validation** | Call a protected endpoint without `X-API-Key` | 1. 401 Unauthorized returned<br>2. No data leaked<br>3. Audit log of failed attempt | ☐ |
| A2 | **PULSE_SECRET validation** | Call /api/pulse without `x-pulse-secret` | 1. 401 returned<br>2. No access to pulse functionality | ☐ |
| A3 | **Frontend auth** | Try to access dashboard without auth | 1. Redirected to login<br>2. No protected data rendered | ☐ |

---

## VERIFICATION QUERIES

Use these SQL queries to verify test outcomes directly in Supabase:

### Ingestion Layer
```sql
-- Check raw_dumps for a specific message
SELECT id, message_text, classification, status, created_at
FROM raw_dumps
WHERE message_text ILIKE '%pricing proposal%'
ORDER BY created_at DESC;

-- Check memories created from notes
SELECT id, content, memory_type, source, created_at
FROM memories
WHERE content ILIKE '%API-first%'
  AND memory_type = 'note';
```

### Processing Layer
```sql
-- Task creation with all fields
SELECT id, title, status, priority, deadline, project_id, organization_id,
       google_event_id, google_task_id, recurrence, is_current, version
FROM tasks
WHERE title ILIKE '%pricing review%'
ORDER BY created_at DESC;

-- Check version chain
SELECT id, title, status, version, is_current, supersedes_id
FROM tasks
WHERE title ILIKE '%pricing review%'
ORDER BY version;

-- Outcome memory
SELECT id, content, memory_type, metadata
FROM memories
WHERE memory_type = 'outcome'
  AND content ILIKE '%pricing review%';
```

### Knowledge Graph
```sql
-- Graph nodes for a project
SELECT id, label, type, db_record_id, is_current
FROM graph_nodes
WHERE label ILIKE '%Solvstrat%'
   OR label ILIKE '%Equisoft%';

-- Edges between entities
SELECT ge.*, 
       sn.label AS source_label, tn.label AS target_label
FROM graph_edges ge
JOIN graph_nodes sn ON ge.source_node_id = sn.id
JOIN graph_nodes tn ON ge.target_node_id = tn.id
WHERE sn.label ILIKE '%Solvstrat%'
   OR tn.label ILIKE '%Solvstrat%';

-- Pending edges awaiting approval
SELECT pe.*, 
       sn.label AS source_label, tn.label AS target_label
FROM pending_graph_edges pe
JOIN graph_nodes sn ON pe.source_node_id = sn.id
JOIN graph_nodes tn ON pe.target_node_id = tn.id
WHERE pe.status = 'pending';

-- Pending nodes
SELECT * FROM pending_nodes
WHERE status = 'pending'
ORDER BY created_at DESC;
```

### Enrichment Queue
```sql
-- Pending enrichment jobs
SELECT * FROM pending_enrichment_jobs
ORDER BY created_at DESC;

-- Check for stuck jobs
SELECT * FROM pending_enrichment_jobs
WHERE status = 'processing'
  AND claimed_at < NOW() - INTERVAL '30 minutes';

-- DLQ items
SELECT * FROM failed_queue
ORDER BY failed_at DESC;
```

### Conversation Threads
```sql
-- Active threads
SELECT id, thread_type, entity_type, entity_label, active_anchor
FROM conversation_threads
WHERE archived_at IS NULL
ORDER BY last_active_at DESC;

-- Active workflows
SELECT * FROM conversation_workflows
WHERE status = 'active';
```

### Decisions
```sql
-- Recent decisions
SELECT id, decision_type, title, status, source_ref
FROM decisions
ORDER BY decided_at DESC
LIMIT 20;
```

### Google Sync
```sql
-- Tasks with Google sync status
SELECT id, title, status, google_event_id IS NOT NULL AS has_calendar,
       google_task_id IS NOT NULL AS has_task
FROM tasks
WHERE google_event_id IS NOT NULL
   OR google_task_id IS NOT NULL
ORDER BY updated_at DESC;
```

### Versioning
```sql
-- Check version counts per task
SELECT COUNT(*) AS versions, MAX(version) AS latest_version
FROM tasks
WHERE is_current = false;
```

### Health Check
```sql
-- Dead letter queue
SELECT COUNT(*) AS dlq_count FROM failed_queue;

-- Stuck dumps
SELECT COUNT(*) AS stuck_dumps
FROM raw_dumps
WHERE status = 'processing'
  AND updated_at < NOW() - INTERVAL '10 minutes';

-- Missing embeddings
SELECT COUNT(*) AS null_embeddings
FROM memories
WHERE embedding IS NULL
  AND created_at > NOW() - INTERVAL '7 days';
```

---

## TEST RUN TRACKING

### Run Log

| Date | Layer | Scenarios Run | Passed | Failed | Notes |
|------|-------|---------------|--------|--------|-------|
| Jul 22, 2026 | All 6 | 133 (non-HITL) | 128 | 5 (3 transient, 1 HITL, 1 transient) | First full UAT run. 9 test script bugs found & fixed (wrong column names, imports). 5 failures: T9 (HITL skipped), D1/D3/N2/R1 (transient Supabase disconnect) |
| Jul 22, 2026 | Layer 1 | 41 (non-HITL) | 39 | 2 (1 HITL, 1 transient) | Re-run after bug fixes. T9 (HITL), D1 (transient disconnect). Cleanup verified. |

**Test Data Cleanup**: 333 Google Tasks deleted (311 [UAT] + 22 TEST/DIAG/SIM). 77 DB rows cleaned. 0 calendar events found.

### Priority Order

1. **Layer 1 (Ingestion)** — T1-T10, V1-V3, D1-D6, W1-W5 (must pass first — foundation)
2. **Layer 2 (Processing)** — P1-P12, N1-N3, Q1-Q2 (task lifecycle is core)
3. **Layer 5.1 (Telegram)** — TE1-TE7 (primary interaction channel)
4. **Layer 3 (Intelligence)** — G1-G8, R1-R3, C1-C3 (knowledge system)
5. **Layer 4 (Presentation)** — B1-B3, D1-D4, S1-S3, H1-H2 (automated delivery)
6. **Layer 5.2 (Web UI)** — U1-U10 (dashboard)
7. **Layer 6 (Infrastructure)** — GC1-GC5, GT1-GT3, DL1-DL2, SM1-SM3, TL1-TL2, A1-A3
8. **Layer 5.3 (Flutter)** — F1-F4 (mobile app)

---

## QUICK REFERENCE: TEST COUNTS

| Layer | Section | Count |
|-------|---------|-------|
| 1. Ingestion | Telegram Text (T) | 10 |
| 1. Ingestion | Telegram Voice (V) | 3 |
| 1. Ingestion | Document Capture (D) | 6 |
| 1. Ingestion | Web UI (W) | 5 |
| 1. Ingestion | Email (E) | 5 |
| **Layer 1 Total** | | **29** |
| 2. Processing | Task Lifecycle (P) | 12 |
| 2. Processing | Note Lifecycle (N) | 3 |
| 2. Processing | Enrichment Queue (Q) | 2 |
| **Layer 2 Total** | | **17** |
| 3. Intelligence | Knowledge Graph (G) | 8 |
| 3. Intelligence | Associative Retrieval (R) | 3 |
| 3. Intelligence | Context Registry (C) | 3 |
| **Layer 3 Total** | | **14** |
| 4. Presentation | Briefing (B) | 3 |
| 4. Presentation | Decision Pulse (D) | 4 |
| 4. Presentation | Sentinel (S) | 3 |
| 4. Presentation | Health Monitor (H) | 2 |
| **Layer 4 Total** | | **12** |
| 5. Surface | Telegram (TE) | 7 |
| 5. Surface | Web UI (U) | 10 |
| 5. Surface | Flutter (F) | 4 |
| **Layer 5 Total** | | **21** |
| 6. Infrastructure | Google Calendar (GC) | 5 |
| 6. Infrastructure | Google Tasks (GT) | 3 |
| 6. Infrastructure | Dead Letter Queue (DL) | 2 |
| 6. Infrastructure | State Machine (SM) | 3 |
| 6. Infrastructure | Temporal Lineage (TL) | 2 |
| 6. Infrastructure | Auth/Security (A) | 3 |
| **Layer 6 Total** | | **18** |
| **GRAND TOTAL** | | **111** |

---

## APPENDIX A: GAP ANALYSIS — Components NOT in the Original Plan

After a thorough codebase audit (scanning all 100+ core modules), the following
components and flows were **missing** from the initial 111-scenario plan. They
are grouped by severity.

### 🔴 Critical Gaps (High Risk — Should Test Before Production Use)

| # | Component | What's Missing | Why It Matters |
|---|-----------|----------------|----------------|
| GAP1 | **WhatsApp ingestion** | Full pipeline: message arrives via MacroDroid → `process_whatsapp_message()` → batch RPC (`batch_whatsapp_message`) → classified → pending decision → approve/reject | WhatsApp is a primary input channel, 15-20 messages per session |
| GAP2 | **Call recording ingestion** | Full pipeline: `.mp4` lands on Google Drive → `call_ingest.py` picks it up → Gemini transcribes → extracts action items → pending decision | Meeting notes extraction — high-value data loss if broken |
| GAP3 | **Teams ingestion** | Full pipeline: Microsoft Teams message → SharePoint attachment → Gemini classification → pending decision | Missing entirely from original plan |
| GAP4 | **Google Sheets Journal Pipeline** | `archive_ingest.py`: fetches Google Form responses → extracts 15+ metadata fields → embeds → stores as memory → creates graph edges. Also: hindsight retrieval in briefings | Powers the COMPASS opening "Drained day yesterday..." context — core to briefing quality |
| GAP5 | **Email ingestion (fetch+classify)** | `email_ingest.py` / `outlook_ingest.py`: actual Gmail/Outlook API fetch, Gemini classification (actionable/fyi/ignored), person linking, draft generation | Original plan only covers approve/reject of already-pending items — not the fetch+classify pipeline |
| GAP6 | **Brain synthesis / canonical pages** | `brain_synth_v2.py`: overnight knowledge consolidation — queries stale canonical pages, gathers fragments from 6 sources, sends to Gemini, versioned write with safety guards | Knowledge graph quality degrades over time if this breaks silently |
| GAP7 | **Pattern learning + auto-decisions** | `patterns.py`: `detect_completion_patterns()` → `format_patterns_for_briefing()` / `format_patterns_for_serendipity()`. Auto-approve logic in `decision_pulse.py` with pattern confidence matching | Auto-approve is how high-confidence edges skip the queue — if broken, all edges require manual approval |
| GAP8 | **Research agent** | `research_agent.py`: Jina AI web search → Gemini dossier synthesis → stored in raw_dumps → shown in next briefing | Only path for external web research into the system |
| GAP9 | **Zombie recovery** | `zombie_recovery()` in `db.py`: resets stuck `processing_completion` dumps back to `pending` after >10 min | Without this, a single crash can permanently orphan a task in "processing" state |
| GAP10 | **Push notifications end-to-end** | `push_notification.py` FCM fire-and-forget on every `send_telegram()` call, sentinel nudges (≤15min), delegation alerts | Flutter app depends on this for real-time alerts |
| GAP11 | **Classifier feedback loop** | `feedback_loop.py` → `ingest_feedback_overrides()` → `classifier_corrections` table. Corrections injected into classify prompt as LEARNED CORRECTIONS. Runs as sentinel piggyback | Without this, classifier never improves from misclassifications |
| GAP12 | **Merge proposals E2E** | Merge proposal created → `merge_proposals` table → appears in Decisions UI → approve (merge nodes, consolidate edges) → reject (standalone creation). NLP correction for type | Data quality suffers if merge logic is broken — duplicate entities accumulate |
| GAP13 | **Clarification loop E2E** | Ambiguous entity → `evaluate_node()` / `evaluate_edge()` → clarification question via Telegram → user replies with shortcode → `handle_response()` → entity resolved → approved | Primary flow for disambiguating new people/orgs |

### 🟡 Medium Gaps (Should Test, Lower Impact)

| # | Component | What's Missing |
|---|-----------|----------------|
| GAP14 | **Post-event capture prompts** | Sentinel fires "Meeting just ended: X" 5-30min after calendar event ends |
| GAP15 | **Weekly sweep** (Sunday) | Sentinel lists stale tasks (>14d), unanswered clarifications, pending graph nodes/edges |
| GAP16 | **Pattern detection piggyback** (Sunday) | `detect_completion_patterns()` → stores to `core_config` for next briefing |
| GAP17 | **Delegation stale alert** | Sentinel flags waiting_on tasks stale >3d, pushes Telegram + FCM alert |
| GAP18 | **Priority auto-escalation** | Sentinel auto-urgents important tasks >7d old (runs every 6h) |
| GAP19 | **Follow-up auto-cancel** | Sentinel auto-cancels waiting_on tasks >14d stale (runs every 12h) |
| GAP20 | **Project creation signals consumer** | Sentinel alerts when tasks reference orgs not in the `organizations` table |
| GAP21 | **Graph integrity sweep** (sentinel) | Copy approved pending edges to graph_edges, validate node IDs, archive terminal edges |
| GAP22 | **Orphan recurring events cleanup** | Sentinel deletes Google Calendar events for cancelled recurring tasks with orphaned event IDs |
| GAP23 | **Memory indexing queue** | `pending_retrieval_index_jobs` → sentinel piggyback processes 2 at a time |
| GAP24 | **DLQ consumer retry + escalation** | `process_dlq()`: exponential backoff, 3 retries → escalation path |
| GAP25 | **Multiple Gemini API key failover** | When key1 returns 429 → rotate to key2 → key3 |
| GAP26 | **Rate limiter + circuit breaker** | `rate_limiter.py` — asyncio.Lock + Redis-based rate limiting for Gemini API calls |
| GAP27 | **Concurrent sentinel safety** | Atomic claim via `claim_pending_enrichment_job` RPC + `claim_pending_index_job` RPC — prevents double-processing if two sentinels run simultaneously |
| GAP28 | **Cache invalidation** | Task/note status changes invalidate context_provider caches (tasks, recent_tasks) |
| GAP29 | **Auto-expiry of past-due recurring tasks** | `_auto_expire_recurring_tasks()` in pulse engine checks RRULE UNTIL/COUNT and marks expired series as cancelled |
| GAP30 | **After-action report / night pulse** | Night Pulse mode generates reflection memory for next day's hindsight |
| GAP31 | **Season context expiry** | Season with `[EXPIRY: YYYY-MM-DD]` → pulse detects expiry → "CRITICAL: Season Context EXPIRED" |
| GAP32 | **Practice detection + correlation** | `detect_practices()` — embedding clustering → practice created as graph_node. `build_practice_correlations()` — correlations with task completion |
| GAP33 | **Habits UI lifecycle** | Web UI Habits tab — weekly grid, completion tracking, dormant → inactive lifecycle |
| GAP34 | **Serendipity engine** | Cross-domain connection detection: keyword overlap + person-in-resource + temporal cluster |
| GAP35 | **Memory clustering** | `discover_new_clusters()` — cluster memories by entity/topic overlap |
| GAP36 | **NotebookLM sync** | `sync_notebooklm_docs.py` — creates/updates Google Docs for Notebook LM integration |
| GAP37 | **Google Drive webhook** (call recordings) | `/api/webhook` with `X-Goog-Channel-Token` validation for Drive file notifications |
| GAP38 | **Email draft send from Web UI** | Full draft approval → send via Gmail/Outlook API flow |
| GAP39 | **Data deletion safety enforcement** | The "never delete records without asking" rule — verify guardrails in code |
| GAP40 | **Google OAuth token refresh** | `get_google_creds()` — refresh token flow when access token expires (all Google APIs) |

### 🟢 Minor Gaps (Nice-to-Have Verification)

| # | Component | What's Missing |
|---|-----------|----------------|
| GAP41 | **Web UI QuickCommand** (already in W3-W5) | Already covered but verify all 3 modes |
| GAP42 | **Pulse engine parallelism** | `asyncio.gather()` for Phase 1 + Phase 2 context assembly in briefing.py |
| GAP43 | **Write-behind pattern** in briefing | Briefing written to raw_dumps before sending to Telegram |
| GAP44 | **Timezone handling** | All timestamps use IST format_rfc3339; test a timezone edge case (DST, UTC vs IST) |
| GAP45 | **Multi-intent messages** (partial) | T10 covers this — but verify secondary_actions also work for non-workflow messages |
| GAP46 | **Email only-mode** | What happens when Gmail API is down vs Outlook-only mode |
| GAP47 | **Empty state handling** | All Web UI modules when no data exists (already partially in U1-U10) |

### Updated Test Counts

| Layer | Original Count | Gaps Added | Revised Total |
|-------|---------------|------------|---------------|
| 1. Ingestion | 29 | +6 (WhatsApp, Calls, Teams, Journal, Email fetch, NotebookLM) | **35** |
| 2. Processing | 17 | +5 (Clarification, Merge, Auto-expiry, Cache invalidation, Zombie recovery) | **22** |
| 3. Intelligence | 14 | +17 (Brain synth, Patterns, Serendipity, Clustering, Practice, Feedback loop, Research agent, Auto-decisions, Merge proposals, Index queue×2) | **31** |
| 4. Presentation | 12 | +17 (All sentinel piggybacks: post-event, weekly sweep, pattern detection, delegation, escalation, auto-cancel, signals, graph sweep, orphan cleanup, after-action report, season expiry) | **29** |
| 5. Surface | 21 | +5 (Push notification E2E, /undo, Practice UI, Habits UI, Email draft send) | **26** |
| 6. Infrastructure | 18 | +8 (API key failover, Rate limiter, Concurrent safety, OAuth refresh, Data deletion, Drive webhook, Timezone edge cases, Email-only mode) | **26** |
| **TOTAL** | **111** | **+58** | **169** |

---

## APPENDIX B: Complete Scenario Index

### Layer 1: Ingestion (35 scenarios)

**1.1 Telegram Text (T1-T14)** — 14  
**1.2 Telegram Voice (V1-V3)** — 3  
**1.3 Document Capture (D1-D6)** — 6  
**1.4 Web UI QuickChat/Command (W1-W5)** — 5  
**1.5 Email Ingestion (E1-E5)** — 5  
**1.6 WhatsApp Ingestion (WA1-WA3)** — 3 *(NEW)*  
**1.7 Call Recording (CR1-CR2)** — 2 *(NEW)*  
**1.8 Google Sheets Journal (J1-J2)** — 2 *(NEW)*  

### Layer 2: Processing (22 scenarios)

**2.1 Task Lifecycle (P1-P12)** — 12  
**2.2 Note Lifecycle (N1-N3)** — 3  
**2.3 Enrichment Queue (Q1-Q3)** — 3 *(+1: concurrent atomic claim)*  
**2.4 Clarification & Merge (CL1-CL3)** — 3 *(NEW)*  
**2.5 Recovery (Z1)** — 1 *(NEW: zombie recovery)*  

### Layer 3: Intelligence (31 scenarios)

**3.1 Knowledge Graph (G1-G10)** — 10 *(+2: NLP correction, merge acceptance)*  
**3.2 Associative Retrieval (R1-R4)** — 4 *(+1: empty result handling)*  
**3.3 Context Registry (C1-C3)** — 3  
**3.4 Brain Synthesis (BS1-BS2)** — 2 *(NEW)*  
**3.5 Pattern Learning (PL1-PL2)** — 2 *(NEW)*  
**3.6 Feedback Loop (FB1-FB2)** — 2 *(NEW)*  
**3.7 Serendipity & Clustering (SC1-SC2)** — 2 *(NEW)*  
**3.8 Practices (PR1-PR3)** — 3 *(NEW)*  
**3.9 Research Agent (RA1)** — 1 *(NEW)*  
**3.10 Memory Indexing (MI1-MI2)** — 2 *(NEW)*  

### Layer 4: Presentation (29 scenarios)

**4.1 Pulse Briefing (B1-B4)** — 4 *(+1: after-action report)*  
**4.2 Decision Pulse (D1-D4)** — 4  
**4.3 Sentinel Meeting Alarms (S1-S2)** — 2  
**4.4 Sentinel Piggybacks (SP1-SP10)** — 10 *(NEW)*  
**4.5 Health Monitor (H1-H2)** — 2  
**4.6 Season Context (SE1)** — 1 *(NEW)*  

### Layer 5: Surface (26 scenarios)

**5.1 Telegram Interactions (TE1-TE8)** — 8 *(+1: /undo command)*  
**5.2 Web UI Dashboard (U1-U12)** — 12 *(+2: Habits, Practice UI)*  
**5.3 Flutter Mobile (F1-F4)** — 4  
**5.4 Push Notifications (PN1-PN2)** — 2 *(NEW)*  

### Layer 6: Infrastructure (26 scenarios)

**6.1 Google Calendar (GC1-GC6)** — 6 *(+1: OAuth refresh)*  
**6.2 Google Tasks (GT1-GT3)** — 3  
**6.3 Dead Letter Queue (DL1-DL3)** — 3 *(+1: escalation path)*  
**6.4 State Machine (SM1-SM3)** — 3  
**6.5 Temporal Lineage (TL1-TL2)** — 2  
**6.6 Auth & Security (A1-A4)** — 4 *(+1: data deletion safety)*  
**6.7 Resiliency (RS1-RS4)** — 4 *(NEW: failover, rate limiter, concurrent safety, timezone)*  

---

**GRAND TOTAL: ~169 scenarios** (up from original 111)

---

## TEST ENVIRONMENT SETUP

### Prerequisites

Before starting, ensure:
1. ✅ Supabase project accessible (local or production)
2. ✅ Telegram bot configured and active
3. ✅ Google Calendar/Tasks API connected
4. ✅ Web UI reachable (localhost:3000 or production)
5. ✅ Flutter app installed (if testing mobile)
6. ✅ All environment variables set
7. ✅ At least some test data exists (or create some first)

### Cleanup Between Runs

After each test session, clean up test artifacts:
```sql
-- Delete test tasks
DELETE FROM tasks WHERE title ILIKE '[TEST]%';
-- Or: use scripts/cleanup_test_data.py
```

**Recommendation**: Create a dedicated test prefix (e.g., `[UAT]`) and use
`scripts/cleanup_test_data.py` to sweep after each session.

### Existing Coverage

This plan supplements the existing automated test suite (45 test files):
- `tests/unit/` — 14 unit test files (actions, why, suggest, context, etc.)
- `tests/clusters/` — 7 integration test clusters (lineage, dedup, workflows, etc.)
- `tests/sim/` — 9 simulation test files (threads, flows, index queue, etc.)
- `tests/` — 6 standalone test files (retrieval, dispatch, rate_limiter, etc.)

**The manual plan covers what automated tests can't**: real API integrations
(Google, Telegram, email), visual UI verification, multi-step workflows with
real timing, and subjective quality assessment of LLM responses.
