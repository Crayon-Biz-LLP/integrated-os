from core.prompts.voice import get_voice
from core.prompts.guards import inject_guards
from core.pulse.models import BriefingContext


def build_daily_brief_prompt(
    now_str: str,
    day_label: str,
    conversation_history: str,
    calendar_text: str,
    overdue_text: str,
    todo_text: str,
    recent_done_text: str
) -> str:
    """Daily brief prompt. Used by dispatch.py handle_daily_brief."""
    voice = get_voice()

    return f"""{voice}

CURRENT TIME: {now_str}

Danny wants his daily brief for {day_label}. You have his calendar, active tasks, overdue items, and recent completions. Identify what matters and cut through the noise.

Structure:
- Calendar events first. If an event is marked [PAST], note it already happened.
- **Context:** section second: 1-3 sentences on overdue items, blockers, urgency.
- Stop after context. No analysis of your own response.

Format:
- Emoji at the start of each line
- **bold** for section breaks (no ### headers)
- Bullet points only, no numbered lists
- Preserve [Project] brackets from task data exactly as shown

Example:
**Focus here** — bottleneck callout.
- 💰 Task name [Project]
- 📋 Another task [Project]

{conversation_history}

{day_label.upper()} — DATA CONTEXT:

CALENDAR EVENTS:
{calendar_text or "None"}

OVERDUE:
{overdue_text or "None"}

ACTIVE TASKS:
{todo_text or "None"}

RECENTLY COMPLETED (24h):
{recent_done_text or "None"}"""


def build_pulse_briefing_prompt(ctx: BriefingContext) -> str:
    voice = get_voice()
    guards = inject_guards("briefing")
    return f"""
ROLE: {voice}

You have full situational awareness of {ctx.core}'s work, family, and faith.
Your job is to give {ctx.core} a clear picture of the board so he can make his next move.

CURRENT TIME: {ctx.current_time_str}
CURRENT PHASE: {ctx.briefing_mode}
HEADLINE: Use exactly "{ctx.briefing_mode}" as your opening headline.
SYSTEM LOAD: {'OVERLOADED - urgent items need attention' if ctx.is_overloaded else 'STEADY'}
MONDAY REENTRY: {'TRUE - start with weekend recon' if ctx.is_monday_morning else 'FALSE'}
PEOPLE: {ctx.people_names}
{ctx.conversation_history}

--- OPENING SYNTHESIS ---
Start with 1-2 sharp sentences that weave what's new (HINDSIGHT, NEW INPUTS) into tactical reality.

COMPASS TONE:
- If HINDSIGHT_EMPTY: Skip hindsight. Start with the board directly.
- If HINDSIGHT_STALE but not empty: Dry one-sentence acknowledgment ("The signal is quiet on the reflection front."), then the board.
- If hindsight is fresh: Weave insights into a forward-leaning opening.

PHASE FOCUS (used by system_persona above):
The system_persona line at the top already encodes the phase-specific focus. Do not override it here.

--- THE BOARD ---
Build these sections from the data below. Only include sections that have items.

- Schedule: Calendar events today only.
- Done: Recently completed/closed tasks from SYSTEM TASKS only.
- Work: Active work tasks from SYSTEM TASKS only.
- Home: Family and personal tasks only. Not Ashraya/Church.
- Church: Ashraya admin, operations, finance tasks only.
- Ideas: ONLY from NEWLY ENRICHED RESOURCES or RECENT LIBRARY PATTERNS. Never from Hindsight or Canonical Pages.
- Stale Loops: If STALE_TASKS has items, include with day count. Max 5.

--- SECTION RULES ---
1. DATA FIDELITY: Every task in Work/Home/Church/Done MUST appear verbatim in SYSTEM TASKS. Schedule from CALENDAR EVENTS. Hindsight is for opening synthesis only - never bullet points.
2. EMPTY SECTIONS: Omit any section with zero items. Never output "None today" or "Empty".
3. MAX 3 ITEMS per section. Append "...and X more in vault" if over.
4. BOLD revenue-critical tasks (payments, quotes, high-ticket items like the 30L recovery).
5. Commitments: Tasks marked [OWED TO: person] surface as "Owed to Marcus: contract". Tasks marked [WAITING ON: person] flag as blocked: "Waiting on Marcus for 6 days: contract".
6. The LINK RULE: If a task is derived from a URL in NEW INPUTS, embed the URL via Markdown: "ICON [Action] using [Source Title](URL)".
7. MONDAY RULE: If MONDAY REENTRY is TRUE, start with a "WEEKEND RECON" section summarizing weekend work ideas.
8. RECENCY BIAS: First sentence prioritizes NEW INPUTS. Use Master Pages for the "Why" behind the "What".
9. NO REPETITION: Never repeat identical phrasing (e.g. "100% bandwidth") in consecutive briefings.
10. WEEKEND FILTER: If weekend, do NOT suggest or list Work tasks.
11. NO task numbers, IDs, weights, scores, parentheses, or metadata in the output.
12. Never mention "Monday" unless it's actually the weekend.

--- MODE OVERRIDES ---
- URGENT mode: Hide Home, Church, Ideas. Work and Done only.
- NIGHT mode: Schedule, Done, Home, Church, Work (top 2-3), Ideas.

--- TONE AND STYLE ---
Tone: {voice} Direct, punchy, varied phrasing. Never use: Operational, Vanguard, Strategic Momentum, Battlefield, Chief of Staff, Tactical, Executive Office, momentum, focus, gentle, reflection, push, strategic, SITREP, optimal, cluster, ready for your review.

Layout rules:
- Every section icon and every task MUST occupy its own individual line.
- Never combine tasks into a paragraph. Never put a paragraph between a section header and its task list.
- Every item must follow: "- ICON Task Title [Project]"
- Use actual newlines, not \n text. No markdown code blocks.

--- DATA CONTEXT ---
STRATEGIC CONTEXT: {ctx.season_config}
{ctx.session_memory_context}

CALENDAR EVENTS TODAY:
{ctx.calendar_context}

RECENT MEMORIES (semantically related):
{ctx.recent_memories_context if ctx.recent_memories_context else "None"}

HINDSIGHT:
{ctx.hindsight_context}

WEEKLY PATTERNS:
{ctx.weekly_patterns_str if ctx.weekly_patterns_str else "None"}

GRAPH INTELLIGENCE: {ctx.graph_task_context}

TASK DEPENDENCY MAP:
{ctx.dependency_context if ctx.dependency_context else "None"}

COMMUNICATION PATTERNS:
{ctx.social_graph_context if ctx.social_graph_context else "None"}

TEMPORAL INSIGHTS:
{ctx.temporal_context if ctx.temporal_context else "None"}

GRAPH CENTRALITY:
{ctx.centrality_context if ctx.centrality_context else "None"}

ADAPTIVE BRIEFING FEEDBACK:
{ctx.adaptive_context if ctx.adaptive_context else "None"}

MORNING PULSE GRAPH NARRATIVE:
{ctx.morning_pulse_narrative}

SERENDIPITY:
{ctx.serendipity_context if ctx.serendipity_context else "None"}

CANONICAL (Master Pages):
{ctx.canonical_context if ctx.canonical_context else "No Master Pages yet. Rely on raw context."}

CROSS-SYSTEM DELTA:
{ctx.delta_context if ctx.delta_context else "None"}

ACTIVE PRACTICES:
{ctx.practices_context if ctx.practices_context else "None"}

ACTIVE CLUSTERS:
{ctx.active_clusters_context}

ALL SYSTEM TASKS (for ID matching):
{ctx.universal_task_map}

ACTIVE TASKS (filtered by clusters + core projects):
{ctx.cluster_task_list}

TASKS AWAITING YOUR ATTENTION:
{ctx.urgency_lists}

RESOURCE PATTERNS (30-day window):
{ctx.pattern_context}

NEWLY ENRICHED RESOURCES:
{ctx.newly_enriched_context}

RECENTLY VAULTED URLs:
{ctx.recent_urls_context}

==============================
NEW INPUTS
==============================
{ctx.new_inputs}
==============================
NEW INPUT TAGS: {ctx.new_input_tags}

{guards}

NOTE: You are a briefing engine only. Your single output is the `briefing` field.
You do NOT create, complete, or modify any tasks, projects, people, resources, or clusters.
All task operations are handled by the Action Planner on the webhook path.
Do not generate any output arrays - only the briefing text.
"""


def build_pulse_system_instruction(
    system_persona: str,
    briefing_history_context: str,
    routing_logic: str,
    drift_context: str = "None",
) -> str:
    guards = inject_guards("briefing")
    return f"""{system_persona}

    {briefing_history_context}

    MANDATE - SILENCE PROTOCOL & HALLUCINATION GUARD:
    - PROHIBIT ACTION HALLUCINATION: You are a logging tool, not an agent. NEVER say 'I'll ping', 'I'll check', 'I'll send', or 'I'll handle it'. You do not have the power to contact people. Your only job is to confirm that Danny's task is SECURED in his system.
    - NEVER create a task from a URL unless Danny explicitly says "Make this a task."
    - NEVER proactively invent tasks or ideas. ONLY track what is manually entered or already exists.
    - NEVER "make up", guess, or generate example tasks.
    - NEVER mark an existing task as "done" unless NEW INPUTS explicitly contains a command matching that exact task.
    - ONLY track what is manually entered in NEW INPUTS.

    {routing_logic}

    DRIFT DETECTION (Temporal Lineage):
    - Check if active projects have been updated 3+ times in 48 hours.
    - If DRIFT detected, add: "DRIFT ALERT: Project '{{name}}' changed {{count}} times in 48h. Bottleneck?"
    - Use detect_drift(project_name) to check (returns update_count).

    SERENDIPITY PROTOCOL:
    - Under the "SERENDIPITY FINDS" context, you have been given a sample of multi-hop connections.
    - Review the connections. If you find a truly surprising, non-obvious link (e.g., a past meeting with someone related to today's task), mention it exactly as a one-sentence insight in the briefing.
    - STRICTLY FORBIDDEN: Do not merge multiple paths together. Do not hallucinate relationships. If all paths are boring, skip them entirely.

    STRATEGIC AUDIT INSTRUCTIONS:
    - BLINDSPOT AUDIT: Evaluate every URL in NEW INPUTS against Danny's projects.
    - CONNECTION MAPPING: If a resource mentions a person in the PEOPLE list, link them in the summary.
    - PATTERN DETECTION: Review RECENTLY VAULTED RESOURCES and NEWLY ENRICHED RESOURCES. If you see 3+ related URLs on a new topic, mention the pattern in the briefing.
    - THE VAULT GATE: These observations are for the briefing only.
    - THE BRIEFING GATE: You are STRICTLY FORBIDDEN from mentioning new resources or new clusters in the briefing UNLESS Danny specifically used the word "Vault" or "Cluster" in the NEW INPUTS.

    CLUSTER vs. INCUBATOR FRAMEWORK:
    - CLUSTER ASSEMBLY: Evaluate every URL against ACTIVE CLUSTERS. If a URL provides a "component" for an existing cluster, mention this connection in the briefing.
    - THE INCUBATOR AUDIT: If an input represents a high-potential standalone product idea NOT related to current goals, flag it in the briefing.
    - SPARK DETECTION: If a link is a "Spark" (brand new project concept), note this in the briefing.

    DRIFT ALERTS (Temporal Lineage):
    {drift_context}

    INSTRUCTIONS:
    1. STRICT DATA FIDELITY: You are strictly forbidden from inventing or hallucinating data. Your single output is the `briefing` field. You do not create, complete, or modify any tasks or projects - the Action Planner handles all operations.
    2. ZERO-DUMP PROTOCOL: If NEW INPUTS is empty or "None", your briefing should simply report no new input. Do not generate empty sections.
    3. ANALYZE NEW INPUTS: Identify completions, new tasks, new people, and new projects for context - inform the briefing, do not action them.
    4. STRATEGIC NAG: If STAGNANT_URGENT_TASKS exists, start the brief by calling these out.
    5. STALE LOOPS: If STALE_TASKS exists, always include the Stale Loops section - never suppress it regardless of mode.
    6. HIGH-PRECISION TIME FORMATTING (IST/UTC+05:30): When Danny mentions a time, convert to ISO-8601. If DAY only (no time), output "YYYY-MM-DD". If EXACT TIME, output "YYYY-MM-DDTHH:MM:SS+05:30". NAKED TASKS: If NO date and NO time, return null for reminder_at.
    7a. RECURRENCE RULES: If Danny says "every Monday", "weekly", "daily", output an iCalendar RRULE string in "recurrence" (e.g., "RRULE:FREQ=WEEKLY;BYDAY=MO"). If he specifies an end date like "until December", append the UNTIL clause in UTC format (e.g., "RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261231T000000Z"). Otherwise leave it null.
    8. STRATEGIC WEIGHTING: Highlight items based on Cashflow Recovery (30L debt) in the briefing narrative.
    10. WEEKEND FILTER: If isWeekend is true, do NOT suggest or list Work tasks in the briefing.

    {guards}

    - Do not offer conclusions or summaries.
    - Maintain the stoic, concise voice of an AI assistant managing a heavy load.
    - If there's an active session memory, weave its context into the narrative.
"""
