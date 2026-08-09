from core.prompts.guards import inject_guards


def build_classify_intent_prompt(
    text: str,
    time_phase: str,
    core_json: str,
    entities_section: str,
    learned_section: str,
    context_str: str,
    conversation_history: str,
    user_name: str | None = None,
    routing_rules: str | None = None,
    role_update_example: str | None = None,
    night_signoffs: str | None = None,
) -> str:
    """Build the intent-classification prompt (M2/M9.2 de-personalized).

    `user_name` and `routing_rules` come from the tenant's user_settings; when
    omitted they fall back to the env/default values (pre-M2 behaviour).
    `role_update_example` is the data-driven ROLE_UPDATE worked example
    (M9.2) — resolved from the tenant's own graph via
    example_entities.resolve_role_update_example(); when omitted or on any
    error it degrades to a neutral line (never another tenant's data).
    """
    from core.services.user_settings import (
        resolve_night_signoffs,
        resolve_user_name,
        routing_rules_text,
    )
    user_name = user_name or resolve_user_name()
    routing_rules = routing_rules or routing_rules_text()
    if night_signoffs is None:
        # M18c: persona card sign-offs take precedence; the fixed override
        # row (core_config 'night_signoffs') is next; neutral default last.
        # The card is L3 KNOWLEDGE — read through the ContextProvider
        # accessor, never directly at the prompt site (architectural rule,
        # session-notes/72). Byte-identical to pre-M18 when no persona card
        # exists (fail-closed).
        try:
            from core.pulse.context import context_provider

            night_signoffs = (
                context_provider.persona_signoffs_context()
                or resolve_night_signoffs()
            )
        except Exception:
            night_signoffs = resolve_night_signoffs()
    if role_update_example is None:
        from core.services.example_entities import resolve_role_update_example
        role_update_example = resolve_role_update_example()
    guards = inject_guards("classify")
    return f"""{guards}

Message: "{text}"{context_str}{conversation_history}
CURRENT TIME CONTEXT: It's the {time_phase}.
IDENTITY & BUSINESS CONTEXT: {core_json}
{routing_rules}
{entities_section}{learned_section}
Return ONLY valid JSON (no markdown, no explanation):
{{
    "intent": "TASK|COMPLETION|NOTE|NOISE|CLARIFICATION_NEEDED|DELEGATE|QUERY|DECLARE_PRACTICE|DAILY_BRIEF|ROLE_UPDATE",
    "confidence": 0.0-1.0,
    "entity": "INBOX|any of the user's routing domains",
    "title": "extracted task title",
    "time_context": "time info if any",
    "clarification_question": "question if needed",
    "receipt": "Stealth status report (no entity names).",
    "reasoning": "brief logic",
    "person_name": "extracted person name (for ROLE_UPDATE only)",
    "role_title": "role title (their role; for ROLE_UPDATE only)",
    "org_name": "organization name (their org; for ROLE_UPDATE only)",
    "contains_hidden_action": true/false
}}

Rules:
- PERSON QUERIES: Questions like "Who is [name]?" or "What about [name]?" are always QUERY, not NOTE. These ask for information retrieval about a person/entity.
- URL-ONLY MESSAGES: If the message matches the regex ^https?://\\S+$, classify as NOTE with receipt "Repository link logged for the vault." Otherwise, NEVER use this receipt.
- STRICT TITLE FIDELITY: The title field must be a literal extraction of the task as spoken. NEVER add project names, infer entities, or change {user_name}'s wording (e.g., if he says "this OS," do NOT change it to a specific product name).
- PROJECT ROUTING: Follow the PROJECT ROUTING block above (per-user life domains). Assign the matching domain name as `entity`; when nothing matches, use INBOX.
- STATUS vs TASK: Task-referential has-happened actions map to COMPLETION; general wins, observations, and milestones still map to NOTE.
- COMPLETION: If the message describes a task-referential action that closes a specific known item — either past-tense ("Finished the client call", "Done with the pricing page") or imperative ("Close the vendor tasks", "Cancel the pilot project", "Mark the pricing page done", "Mark task N as done" where N is ANY numeric task ID) — classify as COMPLETION. Messages containing "mark task" followed by a NUMBER are ALWAYS task completions, never TASK creations. Extract the closest matching task description into `title`. If the message contains multiple entity references, decisions, or mixed actions beyond just closing tasks, classify it as NOTE instead (the enrichment pipeline will extract the closure as a secondary signal).
- MEETING MINUTES: Structured meeting minutes (attendee lists, agenda sections, key decisions, action items) are always NOTE, never COMPLETION or TASK. Action items within minutes are records of what was agreed, not completion reports. The entire document is a contextual record.
- TASK MANAGEMENT DIRECTIVES: If the message explicitly instructs to close, cancel, or mark-done existing tasks identified by name, person, or project (e.g., "Close the vendor tasks", "Cancel the pilot tasks", "Mark the pricing page done"), classify as COMPLETION. The action describes closing existing items, not creating new ones. Single-word replies like "Done" or "Cancelled" in context of active workflows are handled by the workflow system, not this rule.
- CONTAINS HIDDEN ACTION: If the user's message is a QUERY but ALSO contains an actionable command (like creating, closing, or modifying a task), set "contains_hidden_action" to true. Example: "Who is the vendor contact and close their tasks" -> intent="QUERY", contains_hidden_action=true. If the message is purely informational or just a query, set it to false.
- EXPLANATORY CONTEXT RULE: Do NOT treat explanatory phrases (reasons, justifications, context) as hidden actions. Phrases like "until X", "since Y", "because Z", "so that", "for now", "given that" are CONTEXT that explain the primary intent. They are NOT separate commands.
- CLARIFICATION_NEEDED: Use only when the user requests a meeting, task, or event but omits ALL critical details (time, date, person, project) AND none can be inferred from conversation history. Generate a specific question in `clarification_question`. Example: "Schedule a meeting" with no context → CLARIFICATION_NEEDED. "Set up a meeting with a known person" → TASK. Simple confirmations, ambiguous follow-ups, and single-word replies are handled by the workflow system, not this intent.
- TASK: Any message that implies an action, including adding calendar events, meetings, or recurring meetings (e.g. "Add a meeting every Monday"). Do not require a date or time.
- NOTE: Ideas, insights, or learnings worth remembering.
- MEETING NOTES & OBSERVATIONS: "The client call went well", "sync with the partner team was productive" — if it describes an outcome or observation without closing a specific task → NOTE, not COMPLETION.
- PROJECT UPDATES: "The project timeline is tight", "pricing page still open" — status updates without explicit action → NOTE, not TASK.
- IDEAS: "What if the new product is middleware instead of a full platform?" — speculative or conceptual thoughts → NOTE, not TASK.
- QUERY: The user is asking a question to retrieve information from their past notes, tasks, the vault, OR their schedule/calendar (e.g., "What did the analyst say?", "What's the status of the project?", "Meetings this week?").
- ENTITY-AWARE QUERY: If the message references a KNOWN ENTITY from the list above (especially in MENTIONED ENTITIES), and the sentence structure is interrogative or asks "what about", "status of", "where is", "how is", "tell me about" — classify as QUERY, not TASK or COMPLETION. Questions about known entities are almost always information retrieval, not action items.
- DISAMBIGUATION: If confidence < 0.8 and you're torn between multiple intents, set intent to your best guess and explain the ambiguity in reasoning. For example, if a message could be either a QUERY or a TASK, set intent to your most confident guess and explain the uncertainty.
- CONVERSATION HISTORY: Use the CONVERSATION HISTORY block above to disambiguate vague follow-ups. If {user_name} says "reschedule the 2pm" after discussing calendar, route as TASK. The history tells you what the current topic is.
- DELEGATE: Research, competitor audits, or autonomous web research.
- DECLARE_PRACTICE: If {user_name} says "I want to [activity] every [timeframe]" (like a habit), "I'm going to start [activity]", "Track [activity] for me", "I want to build a practice of [activity]" — classify as DECLARE_PRACTICE. Extract the practice name into the title field. Route to the most relevant entity. NOTE: Explicit requests to schedule meetings or calendar blocks are TASKS, not practices.
- DAILY_BRIEF: {user_name} is asking explicitly for their daily briefing or a "good morning" overview. Examples: "good morning", "what's my day look like?", "give me my daily brief". For specific schedule questions like "meetings today?" or "what's on my calendar?", use QUERY instead. Extract into title: "Daily Briefing". Entity: INBOX.
- ROLE_UPDATE: If {user_name} says "[person] is the [role] of [org]", "update that [person] is [role]", "set [person]'s role to [role] at [org]", or similar role attribution statements — classify as ROLE_UPDATE. Extract person_name (the person's full name), role_title (their role), and org_name (the organization). Use conversation history to resolve pronouns like "he" to person_name. Route entity to the most relevant tag. {role_update_example}

─── RECEIPT FORMATTING ───
- RECEIPT: Confirms the action. Vary your phrasing naturally each time: "Got it — X on your list." / "X is logged." / "Done." / "Added." / "Noted."
- LITERAL SUBJECT: Mirror {user_name}'s verb. 'Check with the client' → "Client check-in logged."
- ZERO DATA LOSS: Never drop qualifiers like 'Canadian project' or 'Zoho API'.
- STEALTH ROUTING: Assign the entity in JSON, never mention it (PROJECT, PERSONAL) in receipt text.
- DATE VERIFICATION: If a time or day is mentioned, include it in the receipt.
- NIGHT SIGN-OFF: Confirm the entry, then a simple sign-off: {night_signoffs}
- TONE GUARD: NEVER use: 'momentum', 'focus', 'gentle', 'reflection', 'push', 'strategic', 'SITREP', 'optimal', 'cluster', 'ready for your review'.
- STRATEGIC CORRECTIONS: If {user_name} starts a message with 'Record this for the Vault', 'Correction for the Historian', or 'Correction of Record', classify it immediately as a NOTE with 1.0 confidence. These are manual strategic overrides and must never be ignored.
- META-SYSTEM CONTENT: Allow content that talks about the user's high-value domains (from the PROJECT ROUTING block) even if the message is long or complex. These are high-value strategic inputs."""
