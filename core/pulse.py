import os
import json
import re
import httpx
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import google.generativeai as genai

# Initialize Clients
# Use SERVICE_ROLE_KEY to bypass RLS for background processing
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), 
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# --- 🛰️ LAYER 1: GOOGLE INTEGRATION HELPERS ---

def get_google_creds():
    """Unified credential handshake for all Google services."""
    return Credentials(
        None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )

def get_tasks_service():
    """Helper to spin up the Tasks engine."""
    return build('tasks', 'v1', credentials=get_google_creds())

def sync_to_calendar(title, start_iso, event_id=None):
    """Creates or UPDATES a 30-minute block on the grid."""
    service = build('calendar', 'v3', credentials=get_google_creds())
    try:
        clean_iso = start_iso.replace('Z', '+00:00')
        start_dt = datetime.fromisoformat(clean_iso)
        end_dt = start_dt + timedelta(minutes=30)
        
        event_body = {
            'summary': f"🔥 CRITICAL: {title}",
            'description': 'Automated via Integrated-OS Sync',
            'start': {'dateTime': start_iso, 'timeZone': 'Asia/Kolkata'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Kolkata'},
            'reminders': {'useDefault': True} 
        }
        
        if event_id:
            # 🔄 UPDATE EXISTING SLOT
            res = service.events().patch(calendarId='primary', eventId=event_id, body=event_body).execute()
            print(f"🔄 Calendar slot edited for {title}")
        else:
            # ✨ CREATE NEW SLOT
            res = service.events().insert(calendarId='primary', body=event_body).execute()
            print(f"📅 New calendar block secured for {title}")
            
        return res.get('id')
    except Exception as e:
        # Fallback: If the event_id was invalid (deleted manually), try creating a fresh one
        if event_id: return sync_to_calendar(title, start_iso, event_id=None)
        print(f"⚠️ Calendar sync failed: {e}")
        return None

def delete_calendar_event(event_id):
    """Removes the protective block from the grid."""
    if not event_id: return
    service = build('calendar', 'v3', credentials=get_google_creds())
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        print(f"🗑️ Calendar event {event_id} removed.")
    except Exception as e:
        # If already deleted manually, Google returns a 410/404; we ignore it.
        print(f"⚠️ Calendar delete failed or already gone.")       

def sync_to_google(service, title=None, due_at=None, task_id=None, status='todo'):
    """Checklist manager with Time Visibility Hack."""
    # 1. Handle Completion/Deletion
    if task_id and (status == 'done' or status == 'cancelled'):
        try:
            service.tasks().patch(tasklist='@default', task=task_id, body={'status': 'completed'}).execute()
            return task_id
        except: return None

    # 2. Preparation: Handle Time Visibility Hack
    if due_at and 'T' in due_at:
        time_str = due_at.split('T')[1][:5] # Extract "09:00"
        if title and f"{time_str}" not in title:
            title = f"🕒 {time_str} | {title}"

    # 3. Format Date for Google
    formatted_date = None
    if due_at:
        # 🛡️ FIX: Ensure there is a 'T' and no spaces
        formatted_date = str(due_at).replace(' ', 'T')
        if 'T' not in formatted_date:
            formatted_date = f"{formatted_date}T09:00:00+05:30"
        
        # Ensure it ends with a timezone or 'Z'
        if not (formatted_date.endswith('Z') or '+' in formatted_date[-6:]):
            formatted_date += "+05:30"

    # 4. Execute API Call
    body = {}
    if title: body['title'] = title
    if formatted_date: body['due'] = formatted_date

    if task_id:
        res = service.tasks().patch(tasklist='@default', task=task_id, body=body).execute()
    else:
        res = service.tasks().insert(tasklist='@default', body=body).execute()
    
    return res['id']

# --- 🛰️ LAYER 2: THE AUTO-ENRICHER ---
async def fetch_url_metadata(url: str):
    """Bypasses Social Media walls to extract real post content."""
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            # 🕵️ Identity: Pretending to be the Twitter/Google bot to get the 'SEO' version of the page
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; Twitterbot/1.0)", 
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
            }
            response = await client.get(url, headers=headers)
            
            if response.status_code == 200:
                html = response.text
                
                # 1. Extract the 'og:title' (The Headline/Author)
                title_match = re.search(r'property=["\']og:title["\'] content=["\'](.*?)["\']', html, re.I)
                title = title_match.group(1).strip() if title_match else "Unknown Post"

                # 2. Extract 'og:description' (This is where the actual Post Content lives!)
                desc_match = re.search(r'property=["\']og:description["\'] content=["\'](.*?)["\']', html, re.I)
                description = desc_match.group(1).strip() if desc_match else ""

                # 🧼 Cleanup: Remove the "X.com" or "LinkedIn" branding from titles
                clean_title = re.sub(r'(\s\|.*|on X:|on LinkedIn:)', '', title).strip()
                
                return {"title": clean_title, "description": description}
                
    except Exception as e:
        print(f"Scraper error for {url}: {e}")
    return {"title": "Unknown", "description": ""}

# 🔴 FIX #1: Security Gatekeeper — auth_secret replaces the unused is_manual_trigger bool
async def process_pulse(auth_secret: str = None):
    try:
        # --- 1.1 SECURITY GATEKEEPER ---
        pulse_secret = os.getenv("PULSE_SECRET")
        if pulse_secret and auth_secret != pulse_secret:
            return {"error": "Unauthorized manual trigger.", "status": 401}

        # --- 1. READ: Fetch everything needed for a full state briefing ---
        dumps_res = supabase.table('raw_dumps').select('id, content').eq('is_processed', False).execute()
        dumps = dumps_res.data or []

        active_tasks_res = supabase.table('tasks').select('id, title, project_id, priority, created_at').not_.in_('status', ['done', 'cancelled']).execute()
        active_tasks = active_tasks_res.data or []

        # 💡 Only silence the tool if BOTH new dumps AND open tasks are empty
        if not dumps and not active_tasks:
            return {"message": "Nothing to process, nothing to nag about. Silence is golden."}

        print(f"🚀 PULSE START: Processing {len(dumps)} new dumps and {len(active_tasks)} active tasks.")

        # Fetch supporting metadata
        core_res = supabase.table('core_config').select('key, content').execute()
        core = core_res.data or []

        projects_res = supabase.table('projects').select('id, name, org_tag').execute()
        projects = projects_res.data or []

        people_res = supabase.table('people').select('name, strategic_weight').execute()
        people = people_res.data or []

        # Fetch Active Missions for Context
        missions_res = supabase.table('missions').select('id, title').eq('status', 'active').execute()
        active_missions = missions_res.data or []
        mission_names = [m['title'] for m in active_missions]

        # --- 🕒 1.2 UNIFIED TIME & DAY INTELLIGENCE (IST) ---
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist_offset)
        day = now.isoweekday()  # Monday=1, Sunday=7
        hour = now.hour

        is_weekend = (day == 6 or day == 7)
        is_monday_morning = (day == 1 and hour < 11)

        if is_weekend:
            briefing_mode = "⚪ CHORES & 💡 IDEAS (Weekend Rest)"
            system_persona = "Focus ONLY on Home, Family, and Chores. Explicitly hide Work tasks. Be relaxed."
        else:
            if hour < 11:
                briefing_mode = "🔴 URGENT: CRITICAL ACTIONS"
                system_persona = "High-energy. Direct focus toward URGENT tasks and high-stakes 'Battlefield' items."
            elif hour < 14:
                briefing_mode = "🟡 IMPORTANT: STRATEGIC MOMENTUM"
                system_persona = "Tactical update. Focus on IMPORTANT tasks and MISSION progress, scaling, and growth projects."
            elif hour < 18:
                briefing_mode = "⚪ CHORES: OPERATIONAL SHUTDOWN"
                system_persona = "Closing loops. Push Danny to close work loops and transition to Father mode. Log pending items."
            else:
                briefing_mode = "💡 IDEAS: MENTAL CLEAR-OUT"
                system_persona = "Relaxed reflection. Focus on new product ideas, strategic trends, and library insights. Prep for sleep."

        # --- 1.3 BANDWIDTH & BUFFER CHECK ---
        is_overloaded = len(active_tasks) > 15

        # --- 1.3.1 STRATEGIC TASK FILTERING ---
        filtered_tasks = []
        for t in active_tasks:
            if t.get('priority') == 'urgent':
                filtered_tasks.append(t)
                continue

            project = next((p for p in projects if p.get('id') == t.get('project_id')), None)
            o_tag = project.get('org_tag') if project else "INBOX"

            if is_weekend:
                if o_tag in ['PERSONAL', 'CHURCH']:
                    filtered_tasks.append(t)
            elif hour < 19:
                if o_tag in ['SOLVSTRAT', 'PRODUCT_LABS', 'CRAYON', 'INBOX']:
                    filtered_tasks.append(t)
            else:
                if o_tag in ['PERSONAL', 'CHURCH']:
                    filtered_tasks.append(t)

        # --- 1.4 CONTEXT COMPRESSION ---
        compressed_tasks_list = []
        for t in filtered_tasks:
            project = next((p for p in projects if p.get('id') == t.get('project_id')), None)
            p_name = project.get('name') if project else "General"
            o_tag = project.get('org_tag') if project else "INBOX"
            compressed_tasks_list.append(f"[{o_tag} >> {p_name}] {t.get('title')} ({t.get('priority')}) [ID:{t.get('id')}]")

        compressed_tasks = " | ".join(compressed_tasks_list)
        universal_task_map = " | ".join([f"[ID:{t.get('id')}] {t.get('title')}" for t in active_tasks])

        # --- 1.5 SEASON EXPIRY LOGIC ---
        season_row = next((c for c in core if c.get('key') == 'current_season'), None)
        season_config = season_row.get('content') if season_row else ''

        expiry_match = re.search(r'\[EXPIRY:\s*(\d{4}-\d{2}-\d{2})\]', season_config)
        system_context = "OPERATIONAL"
        if expiry_match:
            expiry_date_str = expiry_match.group(1)
            expiry_date = datetime.strptime(expiry_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if now > expiry_date:
                system_context = "CRITICAL: Season Context EXPIRED."

        # --- 🛡️ 1.6 THE NAG LOGIC (STAGNANT TASK GUARD) ---
        overdue_tasks = []
        for t in filtered_tasks:
            created_date = datetime.fromisoformat(t.get('created_at').replace("Z", "+00:00"))
            hours_old = (now - created_date).total_seconds() / 3600
            if t.get('priority') == 'urgent' and hours_old > 48:
                overdue_tasks.append(t.get('title'))

        new_inputs_text = "\n---\n".join([d['content'] for d in dumps]) if dumps else "None"    

        # --- 🧭 LAYER 3: SMART PATTERN CONTEXT (Last 30 Days) ---
        # Look back 30 days so patterns can form over time, not just items
        thirty_days_ago = (now - timedelta(days=30)).isoformat()
        
        recent_lib = supabase.table('resources')\
            .select('category, title, created_at')\
            .gt('created_at', thirty_days_ago)\
            .order('created_at', desc=True)\
            .limit(100)\
            .execute()
            
        pattern_context = " | ".join([f"[{r['category']}] {r['title']}" for r in recent_lib.data]) if recent_lib.data else "None"
       
        # --- 🛰️ LAYER 2: URL ENRICHMENT ---
        enriched_links = []
        urls = re.findall(r'(https?://\S+)', new_inputs_text) 
        for url in urls:
            meta = await fetch_url_metadata(url)
            enriched_links.append(f"URL: {url} | Title: {meta['title']} | Snippet: {meta['description']}")
        link_context = "\n".join(enriched_links) if enriched_links else "None" 
        
        # --- 2. THINK Phase ---
        print('🤖 Building prompt...')

        project_names = [p['name'] for p in projects]
        people_names = [p['name'] for p in people]
        compressed_tasks_final = compressed_tasks[:3000]  # Hard limit
        new_inputs_text = "\n---\n".join([d['content'] for d in dumps])
        new_input_summary = " | ".join([d['content'] for d in dumps[:5]])
        current_time_str = now.strftime("%A, %B %d, %Y at %I:%M %p IST")

        prompt = f"""    
        ROLE: Chief of Staff for Danny (Executive Office).
        STRATEGIC CONTEXT: {season_config}
        CURRENT PHASE: {briefing_mode}
        CURRENT TIME: {current_time_str}
        SYSTEM_LOAD: {'OVERLOADED' if is_overloaded else 'OPTIMAL'}
        MONDAY_REENTRY: {'TRUE' if is_monday_morning else 'FALSE'}
        STAGNANT URGENT_TASKS: {json.dumps(overdue_tasks)}
        PERSONA GUIDELINE: {system_persona}
        SYSTEM STATUS: {system_context}

        MANDATE: THE SILENCE PROTOCOL & HALLUCINATION GUARD 
        - NEVER create a task from a URL unless Danny explicitly says "Make this a task."
        - NEVER detect "missions" or "strategic trends" proactively.
        - ONLY track what is manually entered or already exists in the database.
        - If NEW INPUTS is "None" or empty, you MUST return completely empty arrays for `completed_task_ids`, `new_tasks`, `new_projects`, and `resources` [].
        - NEVER "make up", guess, or generate example tasks (e.g., "Pay bills", "Check emails").
        - NEVER mark an existing task as "done" unless NEW INPUTS explicitly contains a command matching that exact task.
        - ONLY track what is manually entered in NEW INPUTS.

        CONTEXT:
        - IDENTITY: {json.dumps(core)}
        - PROJECTS: {json.dumps(project_names)}
        - PEOPLE: {json.dumps(people_names)}
        - ACTIONABLE TASKS (DAY FILTERED): {compressed_tasks_final}
        - ALL SYSTEM TASKS (FOR ID MATCHING): {universal_task_map[:3000]}
        - RECENT LIBRARY PATTERNS: {pattern_context}
        - ENRICHED WEB LINKS: {link_context}
        - NEW INPUTS: {new_inputs_text}

        PROJECT ROUTING LOGIC
        Use this hierarchy to assign NEW_TASKS or match COMPLETIONS:
        1. SOLVSTRAT (CASH ENGINE): Match tasks for Atna.ai, Smudge, new Lead Gen here or new SaaS and technology projects. Goal: High-ticket revenue.
        2. PRODUCT LABS (INCUBATOR): 
            - Match existing: CashFlow+ (Vasuuli), Integrated-OS.
            - Match NEW IDEAS: If the input involves "SaaS research," "New Product concept," "MVPs," or "Validation" that is NOT for a current Solvstrat client, tag as PRODUCT LABS.
            - Goal: Future equity and passive income.
        3. CRAYON (UMBRELLA): Match Governance, Tax, and Legal here.
        4. PERSONAL: Match Sunju, kids, dogs here.
        5. CHURCH: 
            - Note: All church-related activities must map to the project "Church".
        6. MISSION OVERRIDE: If a resource fits an ACTIVE MISSION, prioritize the Mission name over the Project name. 
        7. LINK FIDELITY: Do not "hide" a link inside another project's task without including the clickable URL for the original resource.

        NEW PROJECT CREATION CRITERIA:
        1. Only add to "new_projects" if a COMPLETELY UNKNOWN client or organization is mentioned 

        NEW: RESOURCE CAPTURE LOGIC
        Identify any URLs in the NEW INPUTS. For each URL:
        1. CATEGORIZE: Tag as GITHUB, ARTICLE, X_THREAD, LINKEDIN, or TOOL.
        2. SUMMARIZE: Write a concise, 1-sentence description of the value.
        3. PROJECT MATCH: If the link relates to an existing project (e.g., Crayon or Solvstrat), provide the project name.
        4. Do NOT create a task for these. Just save them to the "resources" array.
        5. STRICT MISSION MATCHING: 
           - ONLY assign a `mission_id` if the resource is a direct "building block" for an ACTIVE MISSION. 
           - If it is just a "cool tool" or "interesting read," you MUST leave `mission_id` as NULL.
           - Do NOT force a match. It is better to have an unmapped resource than a wrongly mapped one.

        STRATEGIC AUDIT INSTRUCTIONS
        1. BLINDSPOT AUDIT: Evaluate every URL in NEW INPUTS against Danny's projects. For every URL, attempt to match it to an EXISTING mission or project.
        2. CONNECTION MAPPING: If a resource mentions a person in the PEOPLE list, link them in the summary.
        3. PATTERN DETECTION: 
          - Review RECENT LIBRARY PATTERNS.
          - If you see 3+ links on a new topic (e.g., "YC Prep" or "Lead Gen"), you MAY suggest a new mission in the `new_missions` JSON array.
          - NEVER dump unrelated links into an existing mission just because it's the only one open.
        4. THE VAULT GATE: These updates go to the DATABASE only.
        5. THE BRIEFING GATE: 
            - You are STRICTLY FORBIDDEN from mentioning new resources or new missions in the briefing UNLESS Danny specifically used the word "Vault" or "Mission" in the NEW INPUTS.
            - If those keywords are absent, categorize them in the background and keep the Telegram brief silent about them.

         MISSION vs. INCUBATOR FRAMEWORK
        1. MISSION ASSEMBLY: Evaluate every URL and Input against ACTIVE MISSIONS. 
           - If a link provides a "component" (tool, code, strategy) for a mission, assign the "mission_name".
        2. THE INCUBATOR AUDIT: If an input represents a high-potential standalone product idea NOT related to current goals:
           - Tag it as project_name: "INCUBATOR".
           - In the "strategic_note", evaluate its "Success DNA" (Market fit/Founder match).
        3. SPARK DETECTION: If a link is a "Spark" (brand new project concept), create a log with entry_type: "SPARK".
        4. AUTO-MISSION DETECTION: If 3+ items in NEW INPUTS or RECENT LIBRARY PATTERNS suggest a cohesive new goal (e.g., "Automate Solvstrat Lead Gen"), add it to the "new_missions" array.

        INSTRUCTIONS:
        1. STRICT DATA FIDELITY: You are strictly forbidden from inventing or hallucinating data to fill the JSON. If there is no explicit command in NEW INPUTS, do nothing.
        2. ZERO-DUMP PROTOCOL: If NEW INPUTS is empty or "None", the "new_tasks", "completed_task_ids", "new_projects", and "new_people" arrays MUST remain 100% empty [].
        3. ANALYZE NEW INPUTS: Identify completions, new tasks, new people, and new projects. Use the ROUTING LOGIC to categorize completions and new tasks.
        4. STRATEGIC NAG: If STAGNANT_URGENT_TASKS exists, start the brief by calling these out. Ask why these ₹30L velocity blockers are stalled.
        5. CHECK FOR COMPLETION: Compare inputs against ALL SYSTEM TASKS to identify IDs finished by Danny.
            - If Danny says he finished or completed a task, mark it as done.
            - If Danny describes a result that fulfills a task's objective (e.g., "The contract is signed" fulfills "Get contract signed"), mark it DONE.
            - If Danny uses the past tense of a task's core action verb (e.g., "Mailed the check" fulfills "Mail the check"), mark it DONE.
            - If the input describes the final step of a process (e.g., "App is on the store" fulfills "Submit app for review"), mark it DONE.
            - If Danny says "Cancel", "Ignore", "Forget", or "Not doing" a task, mark it as cancelled.
            - If Danny indicates he is "skipping," "dropping," or "not doing" something, add the ID to "cancelled_task_ids".
            - If Danny says a task is "on hold," "waiting," or "deferred until [Date/Time]," do NOT mark it as cancelled.
            - Instead, update the `reminder_at` field and keep the status as `todo`.
            - Identify if a task is "Revenue Critical" (anything involving payments, quotes, or ₹30L velocity). Set `is_revenue_critical: true`.
        6. 🕒 HIGH-PRECISION TIME FORMATTING (IST/UTC+05:30):
            - When Danny mentions a time (e.g., "Friday 10am", "Tomorrow morning", "at 4pm"), you MUST convert this into a valid ISO-8601 timestamp.
            - LOCAL TIMEZONE: Use Indian Standard Time (IST), which is UTC+05:30.
            - FORMAT: Use "YYYY-MM-DDTHH:MM:SS+05:30".
            - DEFAULTS: 
                - If "Morning" is mentioned without a time: Use 09:00:00+05:30.
                - If "Evening" is mentioned without a time: Use 18:00:00+05:30.
                - If a day (e.g., "Friday") is mentioned without a time: Use 09:00:00+05:30 on that date.
            - CURRENT REFERENCE: Use the system timestamp provided in the input to calculate relative dates (e.g., "Friday" relative to today).
            - FIELD: Always populate this in the `reminder_at` field of the JSON output.
        7. DYNAMIC TASK MATCHING:
            - Compare inputs against ALL SYSTEM TASKS.
            - If Danny says "I'm done" or "Completed," mark the status as `done`.
            - Every NEW_TASK must now include a `reminder_at` if a time was implied.    
        8. AUTO-ONBOARDING:
            - If a new Client/Project is mentioned, add to "new_projects".
            - If a new Person is mentioned, add to "new_people".
        9. STRATEGIC WEIGHTING: Grade items (1-10) based on Cashflow Recovery (₹30L debt).
        10. WEEKEND FILTER: If isWeekend is true ({is_weekend}), do NOT suggest or list Work tasks. Move work inputs to a 'Monday' reminder.
        11. EXECUTIVE BRIEF FORMAT:
            - HEADLINE RULE: Use exactly "{briefing_mode}".
            - ICON RULES: 🔴 (URGENT), 🟡 (IMPORTANT), ⚪ (CHORES), 💡 (IDEAS).
            - SECTIONS: ✅ COMPLETED, 🛡️ WORK (Hide on weekends), 🏠 HOME, 💡 IDEAS (Only at night pulse).
            - TONE: Match the PERSONA GUIDELINE.
            - INTELLIGENT FILTERING: 
                - If mode is 🔴 URGENT: HIDE the 🏠 HOME, 💡 IDEAS, and new Resources. Focus strictly on 🛡️ WORK and ✅ COMPLETED.
                - If mode is 🟡 IMPORTANT: Prioritize 🚀 MISSION progress and 🛡️ WORK.
                - If mode is 💡 IDEAS: Prioritize the 💡 IDEAS section, Incubator Sparks, and 📚 Library links.
            - SECTION DENSITY: Max 3 items per section. If more exist, append: "...and X more in /library or /vault".
            - TASK SYNTAX: Every item must follow: "- [ICON] [Task Title]". No IDs, weights, or parentheses.
        10. MONDAY RULE: If MONDAY_REENTRY is TRUE, start with a "🛡️ WEEKEND RECON" section summarizing any work ideas dumped during the weekend.
        11. STRICT TASK SYNTAX: 
            - Every single task listed in the briefing MUST follow this exact format: "- [ICON] [Task Title]". 
            - THE LINK RULE: If a task is derived from a URL in NEW INPUTS, you MUST embed that URL into the task title using Markdown: "- [ICON] [Action] using [Source Title](URL)".
            - NEGATIVE CONSTRAINTS: NEVER include task numbers, IDs, weights, scores, parentheses, or metadata in the briefing string. NEVER mention "Monday" unless it is actually the weekend.
        
        OUTPUT JSON SCHEMA (WARNING: ONLY POPULATE ARRAYS IF EXPLICITLY COMMANDED IN NEW INPUTS. OTHERWISE RETURN []):
        {{
            "completed_task_ids": [
                // Example ONLY: {{ "id": 123, "status": "done" }}, {{ "id": 456, "status": "todo", "reminder_at": "2026-03-20T10:00:00+05:30" }}
            ],
            "new_projects": [
                // Example ONLY: {{ "name": "...", "importance": 8, "org_tag": "SOLVSTRAT" }}
            ],
            "new_people": [
                // Example ONLY: {{ "name": "...", "role": "...", "strategic_weight": 9 }}
            ],
            "new_tasks": [
                // Example ONLY: {{ "title": "...", "project_name": "...", "priority": "urgent", "est_min": 15, "reminder_at": "..." }}
            ],
            "resources": [
                // Example ONLY: {{ "url": "...", "title": "...", "summary": "...", "mission_name": "...", "project_name": "...", "strategic_note": "..." }}
            ],
            "logs": [],
            "new_missions": [],
            "briefing": "The formatted text string for Telegram."
        }}
        """

        # --- AI GENERATION ---
        ai_data = {
            "briefing": f"⚠️ FALLBACK MODE\n\n{len(dumps)} new inputs:\n{new_input_summary[:200]}",
            "new_tasks": [], "logs": [], "completed_task_ids": [], "new_projects": [], "new_people": []
        }

        try:
            # 🔴 FIX #2: Model name synced to match pulse.js exactly
            model = genai.GenerativeModel(
                model_name="gemini-3-flash-preview",
                generation_config={"response_mime_type": "application/json"}
            )
            response = model.generate_content(prompt)
            response_text = response.text

            # SUPER ROBUST JSON EXTRACTOR
            json_str = re.sub(r'^```json\n?', '', response_text)
            json_str = re.sub(r'\n?```$', '', json_str).strip()

            # 🟡 FIX #3: Both sanitization steps now present (trailing commas + empty values)
            json_str = re.sub(r',\s*([}\]])', r'\1', json_str)           # Step 1: Trailing commas
            json_str = re.sub(r':\s*([}\]]|$)', r': ""\1', json_str)     # Step 2: Empty/dangling values

            match = re.search(r'\{[\s\S]*\}', json_str)
            if match:
                json_str = match.group(0)

            ai_data = json.loads(json_str)
            print("✅ AI Data Parsed Successfully:", list(ai_data.keys()))

        except Exception as e:
            print("AI JSON Parse Error. Falling back.", e)
            return {"error": "AI response failed validation."}

        # --- 3. WRITE Phase (Database Updates) ---

        tasks_service = get_tasks_service()
        
        # A. BATCH NEW PROJECTS (Deduplicated)
        if ai_data.get('new_projects'):
            valid_tags = ['SOLVSTRAT', 'PRODUCT_LABS', 'PERSONAL', 'CRAYON', 'CHURCH']
            filtered_new_projects = []

            for new_p in ai_data['new_projects']:
                p_name = new_p.get('name', 'Unnamed Project')
                p_tag = new_p.get('org_tag', 'INBOX')
                already_exists = any(
                    p_name.lower() in existing_p['name'].lower() or
                    existing_p['name'].lower() in p_name.lower()
                    for existing_p in projects
                )
                if not already_exists:
                    filtered_new_projects.append({
                        "name": p_name,
                        "org_tag": p_tag if p_tag in valid_tags else 'INBOX',
                        "status": "active",
                        "context": "personal" if p_tag in ['CHURCH', 'PERSONAL'] else "work"
                    })

            if filtered_new_projects:
                p_res = supabase.table('projects').insert(filtered_new_projects).execute()
                if p_res.data:
                    projects.extend(p_res.data)
                    print(f"✅ Created {len(p_res.data)} new entity projects.")

        # B. BATCH NEW PEOPLE
        if ai_data.get('new_people'):
            supabase.table('people').insert(ai_data['new_people']).execute()

        # C. BATCH TASK UPDATES (The Smart Rescheduler)
        if ai_data.get('completed_task_ids'):
            for item in ai_data['completed_task_ids']:
                target_id = item.get('id')
                item_status = item.get('status', 'done')
                new_reminder = item.get('reminder_at')
                
                # 1. Fetch current IDs AND Status
                task_ref = supabase.table('tasks').select('status', 'google_task_id', 'google_event_id', 'title').eq('id', target_id).single().execute()
                
                # Extract data safely
                current_db_status = task_ref.data.get('status') if task_ref.data else None
                g_id = task_ref.data.get('google_task_id') if task_ref.data else None
                e_id = task_ref.data.get('google_event_id') if task_ref.data else None
                task_title = task_ref.data.get('title') if task_ref.data else "Untitled Task"

                # 🛑 THE LOCKDOWN: If the task is already 'done' or 'cancelled', 
                # we ignore any "todo" or "reminder" updates from the AI.
                if current_db_status in ['done', 'cancelled']:
                    print(f"🚫 Task {target_id} ('{task_title}') is already {current_db_status}. Blocking AI resurrection.")
                    continue

                # 2. THE SMART CALENDAR SYNC
                if item_status in ['done', 'cancelled'] and e_id:
                    # ACTION: Completion/Cancellation -> KILL the block
                    delete_calendar_event(e_id)
                    e_id = None
                elif new_reminder:
                    if 'T' in new_reminder:
                        # ACTION: Snooze with TIME -> EDIT or CREATE the block
                        e_id = sync_to_calendar(task_title, new_reminder, event_id=e_id)
                    elif e_id:
                        # ACTION: Snooze to DATE-ONLY -> Remove existing block (not needed anymore)
                        delete_calendar_event(e_id)
                        e_id = None

                # 3. GOOGLE TASKS SYNC (Title/Time Updates)
                if g_id:
                    sync_to_google(tasks_service, title=task_title, task_id=g_id, status=item_status, due_at=new_reminder)

                # 4. SUPABASE UPDATE (Save the new/existing Event ID)
                update_payload = {"status": item_status, "google_event_id": e_id}
                if item_status == 'done': update_payload["completed_at"] = datetime.now(timezone.utc).isoformat()
                if new_reminder: update_payload["reminder_at"] = new_reminder

                supabase.table('tasks').update(update_payload).eq('id', target_id).execute()

        # D. BATCH NEW TASKS (Checklist + Calendar Interruption + ID Tracking)
        if ai_data.get('new_tasks'):
            task_inserts = []

            for task in ai_data['new_tasks']:
                # 1. Project Matching Logic (Existing Logic)
                ai_target = (task.get('project_name') or "").lower()
                project_match = next(
                    (p for p in projects if ai_target in p['name'].lower() or p['name'].lower() in ai_target),
                    None
                )
                if not project_match:
                    project_match = next((p for p in projects if p.get('org_tag') == 'INBOX'), projects[0] if projects else None)

                if project_match:
                    # 2. SYNC TO GOOGLE TASKS (The Checklist)
                    g_id = None
                    e_id = None # Initialize empty Event ID
                    reminder_time = task.get('reminder_at')
                    
                    try:
                        g_id = sync_to_google(
                            tasks_service,
                            title=task.get('title', 'Untitled Task'),
                            due_at=reminder_time
                        )
                        print(f"📡 Google Task Created: {task.get('title')}")
                    except Exception as e:
                        print(f"⚠️ Google Tasks Sync failed: {e}")

                    # 3. STRATEGIC GATE: SYNC TO CALENDAR (The Alarm)
                    # We now CAPTURE the e_id returned by the helper
                    if reminder_time and 'T' in reminder_time:
                        try:
                            e_id = sync_to_calendar(task.get('title'), reminder_time)
                            if e_id:
                                print(f"📅 Calendar block secured: {task.get('title')} [ID: {e_id}]")
                        except Exception as ce:
                            print(f"⚠️ Calendar Sync failed: {ce}")

                    # 4. BUILD SUPABASE PAYLOAD (Including the new Event ID)
                    task_inserts.append({
                        "title": task.get('title', 'Untitled Task'),
                        "project_id": project_match['id'],
                        "priority": (task.get('priority') or 'important').lower(),
                        "status": "todo",
                        "estimated_minutes": task.get('est_min', 15),
                        "google_task_id": g_id,
                        "google_event_id": e_id, # <-- Save this so we can delete it later!
                        "reminder_at": reminder_time,
                        "is_revenue_critical": task.get('is_revenue_critical', False)
                    })

            if task_inserts:
                try:
                    supabase.table('tasks').insert(task_inserts).execute()
                    print(f"✅ Successfully synced {len(task_inserts)} tasks with Calendar tracking.")
                except Exception as e:
                    print(f"❌ Supabase Insert Error: {e}")

        # 🚀 E. BATCH NEW MISSIONS (Updated to include Description)
        if ai_data.get('new_missions'):
            for m in ai_data['new_missions']:
                m_title = m.get('title')
                m_desc = m.get('description', 'Auto-detected strategic pattern.') # Capture the logic!
                
                if not any(m_title.lower() in existing['title'].lower() for existing in active_missions):
                    try:
                        m_res = supabase.table('missions').insert({
                            "title": m_title,
                            "description": m_desc, # Now saving to DB
                            "status": "active"
                        }).execute()
                        if m_res.data:
                            active_missions.extend(m_res.data)
                            print(f"🚀 AUTO-MISSION CREATED: {m_title}")
                    except Exception as e:
                        print(f"Error creating mission: {e}")

        # 🔖 F. BATCH NEW RESOURCES (Indented same level as Mission block)
        if ai_data.get('resources'):
            resource_inserts = []
            for res in ai_data['resources']:
                m_name = (res.get('mission_name') or "").lower()
                mission_match = next((m for m in active_missions if m_name in m['title'].lower()), None)
                
                p_name = (res.get('project_name') or "").lower()
                project_match = next((p for p in projects if p_name in p['name'].lower()), None)
                
                resource_inserts.append({
                    "url": res.get('url'),
                    "title": res.get('title'),
                    "summary": res.get('summary'),
                    "strategic_note": res.get('strategic_note'),
                    "category": res.get('category', 'LINK'),
                    "mission_id": mission_match['id'] if mission_match else None,
                    "project_id": project_match['id'] if project_match else None
                })
            
            if resource_inserts:
                supabase.table('resources').insert(resource_inserts).execute()
                print(f"✅ Vaulted {len(resource_inserts)} resources with Strategic Audit.")

        # G. CLEANUP & LOGS
        if ai_data.get('logs'):
            supabase.table('logs').insert(ai_data['logs']).execute()

        if dumps:
            dump_ids = [d['id'] for d in dumps]
            supabase.table('raw_dumps').update({"is_processed": True}).in_('id', dump_ids).execute()

        briefing_text = ai_data.get('briefing', '')
        if briefing_text:
            briefing_text = re.sub(r'\[?ID:\s*\d+\]?', '', briefing_text, flags=re.IGNORECASE).strip()
            
        # --- 4. SPEAK Phase ---
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

        if telegram_chat_id and briefing_text:
            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": telegram_chat_id,
                "text": briefing_text,
                "parse_mode": "Markdown"
            }
            async with httpx.AsyncClient() as client:
                await client.post(url, json=payload)

        return {"success": True, "briefing": briefing_text}

    except Exception as e:
        print(f"Pulse Critical Error: {e}")
        return {"error": str(e)}