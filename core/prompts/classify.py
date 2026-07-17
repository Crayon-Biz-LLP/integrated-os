from core.prompts.guards import inject_guards

def build_classify_intent_prompt(
    text: str,
    time_phase: str,
    core_json: str,
    entities_section: str,
    learned_section: str,
    context_str: str,
    conversation_history: str
) -> str:
    guards = inject_guards("classify")
    return f"""{guards}

Message: "{text}"{context_str}{conversation_history}
CURRENT TIME CONTEXT: It's the {time_phase}.
IDENTITY & BUSINESS CONTEXT: {core_json}
{entities_section}{learned_section}
Return ONLY valid JSON (no markdown, no explanation):
{{
    "intent": "TASK|COMPLETION|NOTE|PROJECT_UPDATE|NOISE|CLARIFICATION_NEEDED|DELEGATE|QUERY|DECLARE_PRACTICE|DAILY_BRIEF|ROLE_UPDATE",
    "confidence": 0.0-1.0,
    "entity": "SOLVSTRAT|QHORD|PERSONAL|ASHRAYA|INBOX",
    "title": "extracted task title",
    "time_context": "time info if any",
    "clarification_question": "question if needed",
    "receipt": "Stealth status report (no entity names).",
    "possible_intents": ["TASK", "COMPLETION", "NOTE", "PROJECT_UPDATE", "QUERY", "DAILY_BRIEF", "DELEGATE", "DECLARE_PRACTICE", "NOISE", "ROLE_UPDATE"],
    "reasoning": "brief logic",
    "person_name": "extracted person name (for ROLE_UPDATE only)",
    "role_title": "role title like Pastor or Treasurer (for ROLE_UPDATE only)",
    "org_name": "organization name like Ashraya Chennai Central (for ROLE_UPDATE only)",
    "contains_hidden_action": true/false
}}

Rules:
- PERSON QUERIES: Questions like "Who is [name]?" or "What about [name]?" are always QUERY, not NOTE. These ask for information retrieval about a person/entity.
- URL-ONLY MESSAGES: If the message matches the regex ^https?://\\S+$, classify as NOTE with receipt "Repository link logged for the vault." Otherwise, NEVER use this receipt.
- STRICT TITLE FIDELITY: The title field must be a literal extraction of the task as spoken. NEVER add project names, infer entities, or change Danny's wording (e.g., if he says "this OS," do NOT change it to "Qhord OS").
- PROJECT ROUTING: Route tasks about personal finances, bills, home, or family to PERSONAL. Route Ashraya church administration, operations, accounts to ASHRAYA. Route personal spiritual practices (bible reading, prayer, volunteering) to PERSONAL. Only route to CRAYON if it relates to corporate governance, business taxes, or legal compliance. Route tech/client work to SOLVSTRAT.
- STATUS vs TASK: Task-referential has-happened actions map to COMPLETION; general wins, observations, and milestones still map to NOTE.
- PROJECT_UPDATE: If the message contains mixed content like status updates, team changes, finance/invoice mentions, decisions, or meeting fallout. This is a rich, multi-faceted update. Use this instead of COMPLETION if the message describes multiple things happening or includes entities/details, even if one of those things is completing a task.
- COMPLETION: If the message describes a task-referential action that closes a specific known item — either past-tense ("Finished the Vasanth call", "Done with the Qhord pricing") or imperative ("Close the Amita tasks", "Cancel the FC Madras project", "Mark the Qhord pricing done", "Mark task N as done" where N is ANY numeric task ID) — classify as COMPLETION. Messages containing "mark task" followed by a NUMBER are ALWAYS task completions, never TASK creations. Extract the closest matching task description into `title`. If the message contains multiple entity references, decisions, or mixed actions beyond just closing tasks, classify it as PROJECT_UPDATE instead (the enrichment pipeline will extract the closure as a secondary signal).
- MEETING MINUTES: Structured meeting minutes (attendee lists, agenda sections, key decisions, action items) are always NOTE, never COMPLETION or TASK. Action items within minutes are records of what was agreed, not completion reports. The entire document is a contextual record.
- TASK MANAGEMENT DIRECTIVES: If the message explicitly instructs to close, cancel, or mark-done existing tasks identified by name, person, or project (e.g., "Close the Amita tasks", "Cancel the FC Madras tasks", "Mark the Qhord pricing done"), classify as COMPLETION. The action describes closing existing items, not creating new ones. Single-word replies like "Done" or "Cancelled" in context of active workflows are handled by the workflow system, not this rule.
- CONTAINS HIDDEN ACTION: If the user's message is a QUERY but ALSO contains an actionable command (like creating, closing, or modifying a task), set "contains_hidden_action" to true. Example: "Who is Amita and close her tasks" -> intent="QUERY", contains_hidden_action=true. If the message is purely informational or just a query, set it to false.
- EXPLANATORY CONTEXT RULE: Do NOT treat explanatory phrases (reasons, justifications, context) as hidden actions. Phrases like "until X", "since Y", "because Z", "so that", "for now", "given that" are CONTEXT that explain the primary intent. They are NOT separate commands.
- CLARIFICATION_NEEDED: Use this intent when the user asks to schedule a meeting or create a task but omits critical info (like time, date, person, or project) AND that info cannot be inferred from conversation history. Generate a specific question in `clarification_question` asking for the missing detail. Example: "Schedule a meeting" with no time/date/person → CLARIFICATION_NEEDED. "Set up a meeting with Vasanth" with a known person from history → TASK (the history provides context). Do NOT use CLARIFICATION_NEEDED for simple confirmations or ambiguous follow-ups — those are handled by the workflow system.
- TASK: Any message that implies an action, including adding calendar events, meetings, or recurring meetings (e.g. "Add a meeting every Monday"). Do not require a date or time.
NOTE: CLARIFICATION_NEEDED takes priority over TASK when the user requests a meeting, task, or event but provides NO time, date, person, or project AND none of these can be inferred from conversation history. If ANY critical detail is present or inferable, use TASK instead.
- NOTE: Ideas, insights, or learnings worth remembering.
- MEETING NOTES & OBSERVATIONS: "Vasanth call went well", "sync with Ashraya team was productive" — if it describes an outcome or observation without closing a specific task → NOTE, not COMPLETION.
- PROJECT UPDATES: "Qhord timeline is tight", "pricing page still open" — status updates without explicit action → NOTE, not TASK.
- IDEAS: "What if Atna is middleware instead of full platform?" — speculative or conceptual thoughts → NOTE, not TASK.
- QUERY: The user is asking a question to retrieve information from their past notes, tasks, the vault, OR their schedule/calendar (e.g., "What did the analyst say?", "What's the status of Qhord?", "Meetings this week?").
- ENTITY-AWARE QUERY: If the message references a KNOWN ENTITY from the list above (especially in MENTIONED ENTITIES), and the sentence structure is interrogative or asks "what about", "status of", "where is", "how is", "tell me about" — classify as QUERY, not TASK or COMPLETION. Questions about known entities are almost always information retrieval, not action items.
- DISAMBIGUATION: If confidence < 0.8 and you're torn between multiple intents, list alternatives in "possible_intents". For example, if a message could be either a QUERY or a TASK, set intent to your best guess and possible_intents to ["TASK", "QUERY"]. Leave as an empty array if you're confident.
- CONVERSATION HISTORY: Use the CONVERSATION HISTORY block above to disambiguate vague follow-ups. If Danny says "reschedule the 2pm" after discussing calendar, route as TASK. The history tells you what the current topic is.
- DELEGATE: Research, competitor audits, or autonomous web research.
- DECLARE_PRACTICE: If Danny says "I want to [activity] every [timeframe]" (like a habit), "I'm going to start [activity]", "Track [activity] for me", "I want to build a practice of [activity]" — classify as DECLARE_PRACTICE. Extract the practice name into the title field. Route to the most relevant entity. NOTE: Explicit requests to schedule meetings or calendar blocks are TASKS, not practices.
- DAILY_BRIEF: Danny is asking explicitly for his daily briefing or a "good morning" overview. Examples: "good morning", "what's my day look like?", "give me my daily brief". For specific schedule questions like "meetings today?" or "what's on my calendar?", use QUERY instead. Extract into title: "Daily Briefing". Entity: INBOX.
- ROLE_UPDATE: If Danny says "[person] is the [role] of [org]", "update that [person] is [role]", "set [person]'s role to [role] at [org]", or similar role attribution statements — classify as ROLE_UPDATE. Extract person_name (the person's full name), role_title (their role), and org_name (the organization). Use conversation history to resolve pronouns like "he" to person_name. Route entity to the most relevant tag. Example: "Marcus Durai is the Pastor of Ashraya Chennai Central" → intent=ROLE_UPDATE, person_name="Marcus Durai", role_title="Pastor", org_name="Ashraya Chennai Central", entity=ASHRAYA.
- RECEIPT RULE: Receipts must be confirmation-only. Use: '[Subject] logged for [Time/Day].'
- LITERAL SUBJECT RULE: Mirror Danny's verb. (e.g., 'Check with Vasanth' → 'Vasanth check-in logged').
- ZERO DATA LOSS: Never drop qualifiers like 'Canadian project' or 'Zoho API'.
- STEALTH ROUTING: Assign the entity in the JSON, but NEVER mention it (SOLVSTRAT, PERSONAL) in the receipt text.
- DATE HANDSHAKE: If a time or day is mentioned, include it in the receipt for verification.
- If it's night (Phase: night), confirm the entry first, THEN give the sign-off command. (e.g., 'Vasanth check-in logged. Now go be a dad.').
- TONE GUARD: NEVER use: 'momentum', 'focus', 'gentle', 'reflection', 'push', 'strategic', 'SITREP', 'optimal', 'cluster', 'ready for your review'.
- STRATEGIC CORRECTIONS: If Danny starts a message with 'Record this for the Vault', 'Correction for the Historian', or 'Correction of Record', classify it immediately as a NOTE with 1.0 confidence. These are manual strategic overrides and must never be ignored.
- META-SYSTEM CONTENT: Allow content that talks about 'Atna', 'Solvstrat', or 'Qhord' even if the message is long or complex. These are high-value strategic inputs."""
