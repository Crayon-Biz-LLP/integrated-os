# 20. The Email Operating System

Integrated-OS processes emails from two providers (Gmail and Outlook) through a shared classification pipeline, turning incoming messages into tasks, drafts, people records, and relationship memories.

## Gmail Ingestion

Runs 30 minutes before each Pulse briefing via GitHub Actions (`email_ingest.yml`).

### The Pipeline

```
Gmail API (last 48h, inbox/Ashraya labels)
    │
    ├─ NOREPLY filter (short-circuit: automated senders → ignored)
    │
    ├─ fetch full message (headers + body)
    │
    ├─ Gemini classify (3 classes):
    │   ├─ "ignored": automated, noreply, newsletter → skip
    │   ├─ "fyi": human sender, no response needed
    │   │   → add_person_from_email() (blocklist + dedup)
    │   │   → write relationship_note memory if has_memory_value
    │   └─ "actionable": requires response
    │       → add_person_from_email() for sender
    │       → fuzzy match linked_project_name against projects table
    │       → insert email_pending_tasks with duplicate guard
    │       → generate email_draft if needs_draft=true
    │
    └─ insert into emails table with classification + linked IDs
```

### Gemini Classification Output

```json
{
  "classification": "actionable|fyi|ignored",
  "summary": "Brief summary of the email",
  "suggested_task": "Verb-first task or null",
  "needs_draft": true/false,
  "linked_person_name": "Name or null",
  "linked_project_name": "Project or null",
  "is_human_sender": true/false,
  "has_memory_value": true/false
}
```

### Person Creation from Email

When a human sender is identified, `add_person_from_email()`:
1. Checks blocklist (wife, customer, noreply@, etc.)
2. Fetches all existing people, builds name→id map (both raw lowercase and normalized)
3. Matches against both normalized and raw names
4. Inserts with `role=None, strategic_weight=5, source='email_ingest'`
5. Returns existing or new person ID

## Outlook Ingestion

Same schedule, same classification pipeline, but through Microsoft Graph API.

### Key Differences from Gmail

- **Work context**: Classification prompt emphasizes business email context (clients, vendors, team)
- **No person auto-creation**: Only looks up existing people via ilike — never creates new people
- **Same pending task + draft flow**: Duplicate guard, pending tasks, auto-drafts all identical to Gmail

## Draft Generation & Approval Flow

### How Drafts Are Created

When Gemini classifies an email as `actionable` with `needs_draft=true`, the system calls `generate_draft()`:
```python
draft_body = await generate_draft(sender, subject, body)
supabase.table('email_drafts').insert({
    "email_id": email_id,
    "draft_body": draft_body,
    "status": "pending"
}).execute()
```

### Managing Drafts via Telegram

The `/ed` command provides full draft management:

| Command | Action |
|---------|--------|
| `/ed` | Lists all pending drafts with email context |
| `ed approve <id>` | Sends the draft via Gmail API or Outlook Graph API |
| `ed reject <id>` | Discards the draft, sets status='rejected' |
| `ed edit <id> <new text>` | Replaces draft body and re-displays for approval |

### Multi-Provider Sending

When a draft is approved:
- **Gmail**: Handles reply-all CC threading, fetches original Message-ID for `In-Reply-To` / `References` headers
- **Outlook**: Uses Microsoft Graph API `replyAll` endpoint
- **Anti-double-send**: Sets status to 'sent' BEFORE the API call (prevents sending twice if the call takes long)

### Email Send Reliability
```python
# Status set BEFORE API call — not after
supabase.table('email_drafts').update({"status": "sent"}).eq('id', draft_id).execute()
# THEN execute the actual send
```

## Pending Task Approval Flow

Email-suggested tasks appear as pending decisions that the user can approve via:

**Telegram**: Reply `"5 yes"` → `process_email_pending_decision(5, 'approve')`
**Web UI**: Click "Yes" on email pending card

When approved:
1. `is_already_in_tasks_table()` checks for duplicates
   - **Block**: Skip or merge (auto-update existing task title)
   - **Flag**: Insert with `possible_duplicate=true`
   - **Clear**: Insert clean
2. Raw_dump inserted with status='pending', source='email'
3. Raw_dump processed by Quick Process → task created → Google Calendar + Tasks sync

When rejected:
1. Sets `danny_decision='rejected'`
2. Cleans up orphan email_drafts (if any)
3. No task created
