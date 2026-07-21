"""Formal State Machines — single source of truth for all valid status transitions.

Every table's valid statuses and allowed transitions are defined here.
Use guard_is_valid_transition() before any status update.
No more ad-hoc status values — add new statuses here first, then use them.

Tables covered:
  - raw_dumps
  - tasks
  - memories
  - messages
  - pending_nodes
  - merge_proposals
  - pending_graph_edges
  - graph_nodes
  - graph_edges
  - conversations
  - conversation_threads (workflows)
  - decisions
  - email_drafts
  - pending_retrieval_index_jobs
  - pending_graph_clarifications
  - agent_queue
  - call_recordings
  - retrieval_index_runs
"""

from core.lib.audit_logger import audit_log_sync


# ═══════════════════════════════════════════════════════════════════════
# 1. raw_dumps
# ═══════════════════════════════════════════════════════════════════════

RAW_DUMPS_STATUSES = {
    "pending",        # Freshly ingested, awaiting processing
    "staged",         # Quick-process extracted but needs Pulse review
    "processing",     # Actively being processed (atomic lock)
    "synced",         # Fully processed and synced
    "completed",      # Processed (legacy terminal state)
    "abandoned",      # Stale/stuck >24h, cleaned by sentinel or maintenance
    "processing_completion",  # Completion handler has taken ownership
    "awaiting_completion_match",  # Needs user disambiguation for completion
}

RAW_DUMPS_TRANSITIONS = {
    "pending":           {"staged", "processing", "abandoned"},
    "staged":            {"processing", "completed", "abandoned"},
    "processing":        {"synced", "staged", "completed", "processing_completion"},
    "processing_completion": {"completed", "pending", "awaiting_completion_match"},
    "awaiting_completion_match": {"completed", "pending"},
    "synced":            set(),  # terminal
    "completed":         set(),  # terminal
    "abandoned":         set(),  # terminal
}


# ═══════════════════════════════════════════════════════════════════════
# 2. tasks
# ═══════════════════════════════════════════════════════════════════════

TASKS_STATUSES = {
    "todo",             # Needs action
    "in_progress",      # Actively being worked on
    "done",             # Completed (for recurring: skip current instance)
    "cancelled",        # Cancelled (for recurring: ends the entire series)
    "blocked",          # Blocked by dependency
}

TASKS_TRANSITIONS = {
    "todo":         {"in_progress", "done", "cancelled", "blocked"},
    "in_progress":  {"todo", "done", "cancelled", "blocked"},
    "done":         set(),  # terminal (recurring: skip instance; cancelled ends series)
    "cancelled":    set(),  # terminal
    "blocked":      {"todo", "in_progress", "cancelled"},
}


# ═══════════════════════════════════════════════════════════════════════
# 3. memories
# ═══════════════════════════════════════════════════════════════════════

# Note: memories use `is_current` for soft-deletion, not a status field per se.
# But memory_type is set once and never changes.

MEMORY_TYPES = {
    "note",              # General note
    "project_update",    # Project status update
    "relationship_note", # Relationship/context note
    "outcome",           # Task completion outcome
}

# memories use versioning via is_current + version + supersedes_id
# No direct status field; soft-deleted by setting is_current = False


# ═══════════════════════════════════════════════════════════════════════
# 4. messages
# ═══════════════════════════════════════════════════════════════════════

MESSAGES_STATUSES = {
    "pending",        # Awaiting review in Decision Pulse
    "completed",      # Processed
    "failed",         # Processing failed
}

MESSAGES_TRANSITIONS = {
    "pending":   {"completed", "failed"},
    "completed": set(),  # terminal
    "failed":    {"pending"},  # retry
}

MESSAGES_CLASSIFICATIONS = {
    "actionable",  # Requires user action
    "fyi",         # Information only
    "ignored",     # Automated/noise
    "error",       # Classification error
}

MESSAGES_DANNY_DECISIONS = {
    None,          # Not yet reviewed
    "approve",     # Approved for task creation
    "reject",      # Rejected
    "skipped",     # Skipped (merged/duplicate)
    "merged",      # Auto-merged with existing task
}

MESSAGES_CHANNELS = {
    "email", "call", "whatsapp", "teams",
}

# MESSAGES_STATUSES above already covers pending/completed/failed.
# MESSAGES_PROCESSING_STATUSES removed — use MESSAGES_STATUSES instead.


# ═══════════════════════════════════════════════════════════════════════
# 5a. pending_nodes (new table — replaces pending_graph_nodes for node creation)
# ═══════════════════════════════════════════════════════════════════════

PENDING_NODES_STATUSES = {
    "pending",              # Awaiting HITL approval in Decision Pulse
    "approved",             # Approved — graph node + DB record created
    "rejected",             # Rejected
    "awaiting_details",     # Waiting for user context (person role etc.)
    "awaiting_clarification",  # Awaiting disambiguation from clarifier (e.g. duplicate detection)
    "flagged",              # Ungrounded — needs clarification
    "merged",               # Merged into another node
}

PENDING_NODES_TRANSITIONS = {
    "pending":              {"approved", "rejected", "awaiting_details", "awaiting_clarification", "flagged"},
    "approved":             set(),  # terminal
    "rejected":             set(),  # terminal
    "awaiting_details":     {"pending", "approved", "rejected"},
    "awaiting_clarification": {"pending", "approved", "rejected"},
    "flagged":              {"pending", "rejected"},
    "merged":               set(),  # terminal
}

PENDING_NODES_TYPES = {
    "person", "organization", "project", "concept",
    "place", "event", "animal", "emotional_state", "practice",
}


# 5b. merge_proposals (replaces merge_proposed status from the old pending_graph_nodes)
# ═══════════════════════════════════════════════════════════════════════

MERGE_PROPOSALS_STATUSES = {
    "proposed",   # Awaiting approval
    "accepted",   # Merge accepted — nodes consolidated
    "rejected",   # Merge rejected
}

MERGE_PROPOSALS_TRANSITIONS = {
    "proposed":  {"accepted", "rejected"},
    "accepted":  set(),  # terminal
    "rejected":  set(),  # terminal
}


# ═══════════════════════════════════════════════════════════════════════
# 6. pending_graph_edges
# ═══════════════════════════════════════════════════════════════════════

PENDING_GRAPH_EDGES_STATUSES = {
    "pending",     # Awaiting HITL approval
    "approved",    # Approved — edge copied to graph_edges
    "rejected",    # Rejected
}

PENDING_GRAPH_EDGES_TRANSITIONS = {
    "pending":  {"approved", "rejected"},
    "approved": set(),  # terminal
    "rejected": set(),  # terminal
}


# ═══════════════════════════════════════════════════════════════════════
# 7. graph_nodes (is_current = lifecycle)
# ═══════════════════════════════════════════════════════════════════════

GRAPH_NODES_TYPES = {
    "person", "organization", "project", "practice", "entity",
}

GRAPH_NODES_METADATA_STATUSES = {
    "active", "dormant", "dismissed",
}


# ═══════════════════════════════════════════════════════════════════════
# 8. conversation_threads (workflows)
# ═══════════════════════════════════════════════════════════════════════

WORKFLOW_STATUSES = {
    "active",     # Awaiting user input
    "expired",    # Past expires_at without resolution
    "resolved",   # Workflow completed
    "cancelled",  # User cancelled
}

WORKFLOW_TRANSITIONS = {
    "active":    {"expired", "resolved", "cancelled"},
    "expired":   set(),  # terminal
    "resolved":  set(),  # terminal
    "cancelled": set(),  # terminal
}

WORKFLOW_TYPES = {
    "calendar_event",       # Batch enrichment from calendar
    "task_creation",        # Creating a task
    "task_closure",         # Closure confirmation
    "completion_match",     # Completion disambiguation
}


# ═══════════════════════════════════════════════════════════════════════
# 9. decisions
# ═══════════════════════════════════════════════════════════════════════

DECISIONS_STATUSES = {
    "active",        # Auto-decided, awaiting user verification
    "superseded",    # Overridden by a newer decision
    "reversed",      # User explicitly undid the decision
    "expired",       # Decision window passed
}

DECISIONS_TRANSITIONS = {
    "active":      {"superseded", "reversed", "expired"},
    "superseded":  set(),  # terminal
    "reversed":    set(),  # terminal
    "expired":     set(),  # terminal
}

DECISIONS_TYPES = {
    "channel_approval",   # Auto-approved channel item
    "graph_node_approval",  # Auto-approved graph node
    "graph_edge_approval",  # Auto-approved graph edge
    "pattern_learned",    # Pattern auto-learned
}


# ═══════════════════════════════════════════════════════════════════════
# 10. email_drafts
# ═══════════════════════════════════════════════════════════════════════

EMAIL_DRAFTS_STATUSES = {
    "pending",    # Awaiting user review/approval
    "sent",       # Approved and sent
    "rejected",   # User rejected the draft
}

EMAIL_DRAFTS_TRANSITIONS = {
    "pending":  {"sent", "rejected"},
    "sent":     set(),  # terminal
    "rejected": set(),  # terminal
}


# ═══════════════════════════════════════════════════════════════════════
# 11. pending_retrieval_index_jobs
# ═══════════════════════════════════════════════════════════════════════

INDEX_JOB_STATUSES = {
    "pending",      # Queued for processing
    "processing",   # Being processed
    "completed",    # Done
    "retrying",     # Failed, queued for retry
    "dead_letter",  # Exhausted retries
}

INDEX_JOB_TRANSITIONS = {
    "pending":     {"processing"},
    "processing":  {"completed", "retrying", "dead_letter"},
    "completed":   set(),  # terminal
    "retrying":    {"processing", "dead_letter"},
    "dead_letter": set(),  # terminal
}


# ═══════════════════════════════════════════════════════════════════════
# 12. pending_graph_clarifications
# ═══════════════════════════════════════════════════════════════════════

CLARIFICATION_STATUSES = {
    "active",     # Waiting for user response
    "resolved",   # User answered
    "expired",    # TTL passed
}

CLARIFICATION_TRANSITIONS = {
    "active":    {"resolved", "expired"},
    "resolved":  set(),  # terminal
    "expired":   set(),  # terminal
}

CLARIFICATION_TYPES = {
    "node",     # Person/org node clarification
    "edge",     # Edge edit clarification
    "session",  # NLP correction session
}

CLARIFICATION_STEPS = {
    "awaiting_person_context",  # Awaiting person role/org info
    "awaiting_edge_edit",       # Awaiting edge correction
    "collecting_actions",       # Collecting NLP correction actions
}


# ═══════════════════════════════════════════════════════════════════════
# 13. agent_queue
# ═══════════════════════════════════════════════════════════════════════

AGENT_QUEUE_STATUSES = {
    "pending",     # Queued
    "processing",  # Being processed by agent
    "completed",   # Done
    "failed",      # Failed
}

AGENT_QUEUE_TRANSITIONS = {
    "pending":    {"processing"},
    "processing": {"completed", "failed"},
    "completed":  set(),  # terminal
    "failed":     {"pending"},  # retry
}


# ═══════════════════════════════════════════════════════════════════════
# 14. call_recordings
# ═══════════════════════════════════════════════════════════════════════

CALL_RECORDING_STATUSES = {
    "completed",  # Transcribed and extracted
    "failed",     # Processing failed
}

CALL_RECORDING_TRANSITIONS = {
    "completed": set(),  # terminal
    "failed":    set(),  # terminal
}


# ═══════════════════════════════════════════════════════════════════════
# 15. retrieval_index_runs
# ═══════════════════════════════════════════════════════════════════════

INDEX_RUN_STATUSES = {
    "running",     # Active backfill run
    "completed",   # Done
    "failed",      # Failed
    "skipped",     # Skipped (indexing disabled)
    "dry_run",     # Dry run mode
}

INDEX_RUN_TRANSITIONS = {
    "running":   {"completed", "failed"},
    "completed": set(),  # terminal
    "failed":    {"running"},  # retry by re-running
    "skipped":   set(),  # terminal
    "dry_run":   set(),  # terminal
}


# ═══════════════════════════════════════════════════════════════════════
# Guard Functions
# ═══════════════════════════════════════════════════════════════════════

def guard_is_valid_status(table: str, status: str) -> bool:
    """Check if a status value is valid for a given table.

    Tables without a status field (e.g. memories) return True for any value,
    since they use other mechanisms (is_current, memory_type).
    """
    map = {
        "raw_dumps": RAW_DUMPS_STATUSES,
        "tasks": TASKS_STATUSES,
        "messages": MESSAGES_STATUSES,
        "pending_nodes": PENDING_NODES_STATUSES,
        "merge_proposals": MERGE_PROPOSALS_STATUSES,

        "pending_graph_edges": PENDING_GRAPH_EDGES_STATUSES,
        "conversation_workflows": WORKFLOW_STATUSES,
        "conversation_threads": WORKFLOW_STATUSES,
        "decisions": DECISIONS_STATUSES,
        "email_drafts": EMAIL_DRAFTS_STATUSES,
        "pending_retrieval_index_jobs": INDEX_JOB_STATUSES,
        "pending_graph_clarifications": CLARIFICATION_STATUSES,
        "agent_queue": AGENT_QUEUE_STATUSES,
        "call_recordings": CALL_RECORDING_STATUSES,
        "retrieval_index_runs": INDEX_RUN_STATUSES,
    }
    valid = map.get(table)
    if valid is None:
        return True  # Table not tracked — allow (memories, etc.)
    return status in valid


def guard_is_valid_transition(table: str, current: str, next_status: str) -> bool:
    """Validate a status transition.

    Returns True if `next_status` is reachable from `current` for `table`.

    Example:
        guard_is_valid_transition("tasks", "todo", "done") -> True
        guard_is_valid_transition("tasks", "done", "todo") -> False
    """
    map = {
        "raw_dumps": RAW_DUMPS_TRANSITIONS,
        "tasks": TASKS_TRANSITIONS,
        "pending_nodes": PENDING_NODES_TRANSITIONS,
        "merge_proposals": MERGE_PROPOSALS_TRANSITIONS,

        "pending_graph_edges": PENDING_GRAPH_EDGES_TRANSITIONS,
        "conversation_workflows": WORKFLOW_TRANSITIONS,
        "conversation_threads": WORKFLOW_TRANSITIONS,
        "decisions": DECISIONS_TRANSITIONS,
        "email_drafts": EMAIL_DRAFTS_TRANSITIONS,
        "pending_retrieval_index_jobs": INDEX_JOB_TRANSITIONS,
        "pending_graph_clarifications": CLARIFICATION_TRANSITIONS,
        "agent_queue": AGENT_QUEUE_TRANSITIONS,
        "call_recordings": CALL_RECORDING_TRANSITIONS,
        "retrieval_index_runs": INDEX_RUN_TRANSITIONS,
    }
    valid_from = map.get(table)
    if valid_from is None:
        return True  # Table not tracked — allow
    transitions = valid_from.get(current, set())
    return next_status in transitions


def guard_require_valid_transition(table: str, current: str, next_status: str,
                                    record_id=None, context: str = "") -> bool:
    """Wrapper: log and return False on invalid transition.

    Use this at every status update site. If it returns False, the caller
    should NOT proceed with the update.
    """
    if not guard_is_valid_status(table, next_status):
        audit_log_sync("state_machine", "WARNING",
            f"Invalid status '{next_status}' for table '{table}' "
            f"(record_id={record_id}) {context}")
        return False
    if not guard_is_valid_transition(table, current, next_status):
        audit_log_sync("state_machine", "WARNING",
            f"Invalid transition '{current}' -> '{next_status}' for table '{table}' "
            f"(record_id={record_id}) {context}")
        return False
    return True
