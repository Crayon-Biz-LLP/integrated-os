import os
import json
import re
import httpx
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google import genai

# Initialize Clients
# Use SERVICE_ROLE_KEY to bypass RLS for background processing
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), 
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

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

def format_rfc3339(date_str):
    """Ensures a timestamp is 100% compliant with Google's strict RFC-3339 requirements."""
    if not date_str: return None
    # 🛡️ FIX: Replace space with 'T' and ensure IST timezone
    clean = str(date_str).replace(' ', 'T')
    if 'T' not in clean:
        clean = f"{clean}T09:00:00+05:30"
    if not (clean.endswith('Z') or '+' in clean[-6:]):
        clean += "+05:30"
    return clean

def check_conflict(start_iso):
    """Radar: Checks if a 30-minute window is already booked."""
    try:
        service = build('calendar', 'v3', credentials=get_google_creds())
        rfc_time = format_rfc3339(start_iso)
        
        start_dt = datetime.fromisoformat(rfc_time.replace('Z', '+00:00'))
        end_dt = start_dt + timedelta(minutes=30)
        
        events_res = service.events().list(
            calendarId='primary',
            timeMin=rfc_time,
            timeMax=end_dt.isoformat(),
            singleEvents=True
        ).execute()
        
        events = events_res.get('items', [])
        return events[0].get('summary') if events else None
    except Exception as e:
        print(f"⚠️ Conflict check failed: {e}")
        return None

def sync_to_calendar(title, start_iso, event_id=None):
    """Creates or UPDATES a 30-minute block on the grid."""
    service = build('calendar', 'v3', credentials=get_google_creds())
    try:
        rfc_time = format_rfc3339(start_iso)
        start_dt = datetime.fromisoformat(rfc_time.replace('Z', '+00:00'))
        end_dt = start_dt + timedelta(minutes=30)
        
        event_body = {
            'summary': f"🔥 CRITICAL: {title}",
            'description': 'Automated via Integrated-OS Sync',
            'start': {'dateTime': rfc_time, 'timeZone': 'Asia/Kolkata'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Kolkata'},
            'reminders': {'useDefault': True} 
        }
        
        if event_id:
            res = service.events().patch(calendarId='primary', eventId=event_id, body=event_body).execute()
            print(f"🔄 SUCCESS: Calendar slot edited for {title}")
        else:
            res = service.events().insert(calendarId='primary', body=event_body).execute()
            print(f"📅 SUCCESS: New calendar block secured for {title}")
            
        return res.get('id')
    except Exception as e:
        # Fallback logic: If the event_id was invalid, try creating fresh
        if event_id: 
            print(f"⚠️ Event ID {event_id} invalid. Attempting fresh creation...")
            return sync_to_calendar(title, start_iso, event_id=None)
        print(f"❌ CRITICAL: Calendar sync failed: {e}")
        return None

def delete_calendar_event(event_id):
    """Removes the protective block from the grid with explicit logging."""
    if not event_id: return
    service = build('calendar', 'v3', credentials=get_google_creds())
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        print(f"🗑️ SUCCESS: Calendar event {event_id} removed.")
    except Exception as e:
        # Don't use 'pass'—keep the warning so you know if the grid is dirty
        print(f"⚠️ Note: Calendar delete failed (likely already gone).")

def sync_to_google(service, title=None, due_at=None, task_id=None, status='todo'):
    """Checklist Manager: Handles task sync with RFC-3339 guard."""
    # 1. Handle Completion/Deletion
    if task_id and (status == 'done' or status == 'cancelled'):
        try:
            service.tasks().patch(tasklist='@default', task=task_id, body={'status': 'completed'}).execute()
            return task_id
        except: return None

    # 2. Preparation: RFC-3339 Formatting
    rfc_date = format_rfc3339(due_at)
    
    # 3. Time-Visibility Title Hack
    if rfc_date and 'T' in rfc_date:
        time_str = rfc_date.split('T')[1][:5] # Extract "09:00"
        if title and f"{time_str}" not in title:
            title = f"🕒 {time_str} | {title}"

    # 4. Build Body and Execute API Call
    body = {}
    if title: body['title'] = title
    if rfc_date: body['due'] = rfc_date

    try:
        if task_id:
            res = service.tasks().patch(tasklist='@default', task=task_id, body=body).execute()
        else:
            res = service.tasks().insert(tasklist='@default', body=body).execute()
        return res['id']
    except Exception as e:
        print(f"⚠️ Google Tasks API error: {e}")
        return None

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

        active_tasks_res = supabase.table('tasks').select('id, title, project_id, priority, created_at, reminder_at, google_event_id').not_.in_('status', ['done', 'cancelled']).execute()
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

        # --- 1.3.1 STRATEGIC TASK FILTERING (Robust Horizon Guard) ---
        filtered_tasks = []
        horizon_cutoff = now + timedelta(days=2)

        for t in active_tasks:
            raw_reminder = t.get('reminder_at')
            
            if raw_reminder:
                try:
                    # 🛡️ THE CLEANER: Replace space with 'T' and 'Z' with UTC offset
                    clean_reminder = str(raw_reminder).replace(' ', 'T').replace('Z', '+00:00')
                    task_date = datetime.fromisoformat(clean_reminder)
                    
                    # 🛡️ TIMEZONE AWARENESS: Ensure we are comparing Apples to Apples (IST)
                    if task_date.tzinfo is None:
                        task_date = task_date.replace(tzinfo=ist_offset)
                    
                    # 🛡️ THE HORIZON CHECK: If task is > 2 days away, SKIP IT.
                    if task_date > horizon_cutoff:
                        continue 
                except Exception as e:
                    # If it still fails, we log it but keep the task visible for safety
                    print(f"⚠️ Horizon Guard bypassed for '{t.get('title')}': {e}")

            # --- Existing Category Logic ---
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

        # --- 1.4 CONTEXT COMPRESSION & PRUNING ---
        # 🛡️ THE HORIZON GATE (Rule 2)
        horizon_cutoff = now + timedelta(days=2)
        # 🛡️ THE NAG GATE (Rule 1)
        two_weeks_ago = now - timedelta(days=14)
        
        recent_tasks = []
        for t in active_tasks:
            try:
                # 🛡️ RULE 2: If the reminder is more than 48 hours away, HIDE IT FROM THE AI
                raw_remind = t.get('reminder_at')
                if raw_remind:
                    clean_remind = str(raw_remind).replace(' ', 'T').replace('Z', '+00:00')
                    remind_dt = datetime.fromisoformat(clean_remind)
                    if remind_dt > horizon_cutoff:
                        continue # Dawn (May 7) is skipped here!

                # 🛡️ RULE 1: Only show recently created tasks for background context
                created_dt = datetime.fromisoformat(t['created_at'].replace('Z', '+00:00'))
                if created_dt > two_weeks_ago:
                    recent_tasks.append(t)
            except:
                recent_tasks.append(t) # Safety fallback

        # This is the AI's "Visual Field"
        universal_task_map = " | ".join([f"[ID:{t.get('id')}] {t.get('title')}" for t in recent_tasks])

        # B. BUILD COMPRESSED LIST (For the Briefing Context)
        # 🛡️ FIX: Defining 'compressed_tasks' so the prompt builder doesn't crash!
        compressed_tasks_list = []
        for t in filtered_tasks:
            project = next((p for p in projects if p.get('id') == t.get('project_id')), None)
            p_name = project.get('name') if project else "General"
            o_tag = project.get('org_tag') if project else "INBOX"
            compressed_tasks_list.append(f"[{o_tag} >> {p_name}] {t.get('title')} ({t.get('priority')}) [ID:{t.get('id')}]")

        compressed_tasks = " | ".join(compressed_tasks_list)

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
            try:
                raw_created = t.get('created_at')
                if raw_created:
                    # Normalize and compare hours
                    created_date = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
                    hours_old = (now - created_date).total_seconds() / 3600
                    if t.get('priority') == 'urgent' and hours_old > 48:
                        overdue_tasks.append(t.get('title'))
            except Exception as e:
                print(f"⚠️ Nag Logic skipped for task '{t.get('title')}': {e}")

        # --- 🕒 1.7 INPUT PREP ---
        new_inputs_text = "\n---\n".join([d['content'] for d in dumps]) if dumps else "None"    

        # --- 🧭 LAYER 3: SMART PATTERN CONTEXT (Last 30 Days) ---
        # Look back 30 days so patterns can form over time, not just items
        thirty_days_ago = (now - timedelta(days=30)).isoformat()

        recent_lib = supabase.table('resources')\
            .select('url, category, title, created_at')\
            .gt('created_at', thirty_days_ago)\
            .order('created_at', desc=True)\
            .limit(50)\
            .execute()

        # The AI only needs the Category and Title for the prompt context
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
        1. QHORD (THE MISSION): Any task related to the June launch, Joel (Co-founder), GTM strategies, or Qhord product features gets URGENT status and 10/10 strategic weight. Set is_revenue_critical: true for anything involving Pilots or Sales.
        2. SOLVSTRAT (CASH ENGINE): Match tasks for Smudge, new Lead Gen, or high-ticket technology services here. Goal: High-ticket revenue to fuel the Qhord launch.
        3. PRODUCT LABS (INCUBATOR): 
            - Match existing: CashFlow+ (Vasuuli), Integrated-OS.
            - Match NEW IDEAS: If the input involves "SaaS research," "New Product concept," "MVPs," or "Validation" that is NOT for a current Solvstrat client, tag as PRODUCT LABS.
        4. CRAYON (UMBRELLA): Match Governance, Tax, and Legal here.
        5. PERSONAL: Match Sunju, kids, dogs, and home maintenance here.
        6. CHURCH: 
            - Note: All church-related activities MUST map to the project "Church".
        7. MISSION OVERRIDE: If a resource fits an ACTIVE MISSION, prioritize the Mission name over the Project name. 
        8. LINK FIDELITY: Every task derived from a URL MUST include the clickable URL in the title.

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
            response = client.models.generate_content(model="gemini-3-flash-preview", contents=prompt, config={'response_mime_type': 'application/json'})
            response_text = response.text

        except Exception as e:
            print(f"AI JSON Parse Error. Falling back: {e}")
            
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
                raw_reminder = item.get('reminder_at')
                
                # 🛡️ RFC-3339 GUARD: Sanitize the timestamp immediately
                # This fixes the "Space" bug before Google ever sees it
                new_reminder = format_rfc3339(raw_reminder) if raw_reminder else None
                
                # 1. Fetch current IDs AND Status
                task_ref = supabase.table('tasks').select('status', 'google_task_id', 'google_event_id', 'title').eq('id', target_id).single().execute()
                
                # Extract data safely
                current_db_status = task_ref.data.get('status') if task_ref.data else None
                g_id = task_ref.data.get('google_task_id') if task_ref.data else None
                e_id = task_ref.data.get('google_event_id') if task_ref.data else None
                task_title = task_ref.data.get('title') if task_ref.data else "Untitled Task"

                # 🛑 THE LOCKDOWN: Block AI resurrection of finished tasks
                if current_db_status in ['done', 'cancelled']:
                    print(f"🚫 Task {target_id} ('{task_title}') is already {current_db_status}. Skipping.")
                    continue

                # 2. THE SMART CALENDAR SYNC (With Radar)
                if item_status in ['done', 'cancelled'] and e_id:
                    delete_calendar_event(e_id)
                    e_id = None
                elif new_reminder and 'T' in new_reminder:
                    # 🛰️ RADAR: Check for conflict before moving the block
                    conflict_name = check_conflict(new_reminder)
                    if conflict_name:
                        # 🛡️ Safety: Assignment ensures we don't crash if 'briefing' key is missing
                        current_briefing = ai_data.get('briefing', "")
                        ai_data['briefing'] = current_briefing + f"\n\n⚠️ **SNOOZE CONFLICT:** Tried moving '{task_title}' to {new_reminder.split('T')[1][:5]}, but you have '{conflict_name}' then."
                    
                    # Edit or create the block
                    e_id = sync_to_calendar(task_title, new_reminder, event_id=e_id)
                elif e_id:
                    # Snooze to DATE-ONLY -> Remove existing block
                    delete_calendar_event(e_id)
                    e_id = None

                # 3. GOOGLE TASKS SYNC (Uses the same sanitized timestamp)
                if g_id:
                    sync_to_google(tasks_service, title=task_title, task_id=g_id, status=item_status, due_at=new_reminder)

                # 4. SUPABASE UPDATE (Saves 'T' format and allows time removal)
                update_payload = {"status": item_status, "google_event_id": e_id}
                if item_status == 'done': 
                    update_payload["completed_at"] = datetime.now(timezone.utc).isoformat()
                
                # REMOVE the 'if' here to allow clearing the time
                update_payload["reminder_at"] = new_reminder 

                supabase.table('tasks').update(update_payload).eq('id', target_id).execute()

        # D. BATCH NEW TASKS (Checklist + Calendar Interruption + ID Tracking)
        if ai_data.get('new_tasks'):
            task_inserts = []

            for task in ai_data['new_tasks']:
                # 1. High-Precision Project Matching Logic
                ai_target = (task.get('project_name') or "").lower()
                
                # 🛡️ STEP A: Try for an EXACT match first
                project_match = next((p for p in projects if ai_target == p['name'].lower()), None)
                
                # 🛡️ STEP B: Fuzzy match ONLY if ai_target isn't empty
                if not project_match and ai_target.strip():
                    project_match = next(
                        (p for p in projects if ai_target in p['name'].lower() or p['name'].lower() in ai_target),
                        None
                    )
                
                # 🛡️ STEP C: The Safety Net (Default to INBOX)
                if not project_match:
                    project_match = next((p for p in projects if p.get('org_tag') == 'INBOX'), projects[0] if projects else None)

                if project_match:
                    # 🛡️ RFC-3339 GUARD: Sanitize the AI's time string immediately
                    raw_time = task.get('reminder_at')
                    sanitized_time = format_rfc3339(raw_time) if raw_time else None
                    
                    g_id = None
                    e_id = None
                    task_title = task.get('title', 'Untitled Task')

                    # 2. SYNC TO GOOGLE TASKS (The Checklist)
                    try:
                        g_id = sync_to_google(
                            tasks_service,
                            title=task_title,
                            due_at=sanitized_time
                        )
                        if g_id: print(f"📡 Google Task Created: {task_title}")
                    except Exception as e:
                        print(f"⚠️ Google Tasks Sync failed for {task_title}: {e}")

                    # 3. STRATEGIC GATE: SYNC TO CALENDAR (The Radar + Alarm)
                    if sanitized_time and 'T' in sanitized_time:
                        try:
                            # 🛰️ RADAR: Check for clash
                            conflict_name = check_conflict(sanitized_time)
                            if conflict_name:
                                briefing = ai_data.get('briefing', "")
                                ai_data['briefing'] = briefing + f"\n\n⚠️ **CALENDAR CLASH:** '{task_title}' overlaps with '{conflict_name}'."
                            
                            # Secure the block
                            e_id = sync_to_calendar(task_title, sanitized_time)
                            if e_id: print(f"📅 Calendar block secured: {task_title}")
                            
                        except Exception as ce:
                            print(f"⚠️ Calendar Sync failed for {task_title}: {ce}")

                    # 4. BUILD SUPABASE PAYLOAD (Using the Sanitized Time)
                    task_inserts.append({
                        "title": task_title,
                        "project_id": project_match['id'],
                        "priority": (task.get('priority') or 'important').lower(),
                        "status": "todo",
                        "estimated_minutes": task.get('est_min', 15),
                        "google_task_id": g_id,
                        "google_event_id": e_id,
                        "reminder_at": sanitized_time, # Store 'T' format in DB
                        "is_revenue_critical": task.get('is_revenue_critical', False)
                    })

            if task_inserts:
                try:
                    supabase.table('tasks').insert(task_inserts).execute()
                    print(f"✅ Successfully synced {len(task_inserts)} new tasks.")
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

        # 🔖 F. BATCH NEW RESOURCES (Titan-Grade De-Duplication)
        if ai_data.get('resources'):
            resource_inserts = []
            # 🛡️ THE GUARD: Now works because we added 'url' to the select query above
            existing_urls = {r['url'] for r in recent_lib.data} if recent_lib.data else set()

            for res in ai_data['resources']:
                target_url = res.get('url')
                
                if target_url in existing_urls:
                    print(f"⏩ Skipping duplicate resource: {target_url}")
                    continue

                # --- High-Precision Matching (No Empty Strings!) ---
                m_name = (res.get('mission_name') or "").lower().strip()
                mission_match = next((m for m in active_missions if m_name == m['title'].lower()), None)
                if not mission_match and m_name:
                    mission_match = next((m for m in active_missions if m_name in m['title'].lower()), None)
                
                p_name = (res.get('project_name') or "").lower().strip()
                project_match = next((p for p in projects if p_name == p['name'].lower()), None)
                if not project_match and p_name:
                    project_match = next((p for p in projects if p_name in p['name'].lower()), None)
                
                resource_inserts.append({
                    "url": target_url,
                    "title": res.get('title'),
                    "summary": res.get('summary'),
                    "strategic_note": res.get('strategic_note'),
                    "category": res.get('category', 'LINK'),
                    "mission_id": mission_match['id'] if mission_match else None,
                    "project_id": project_match['id'] if project_match else None
                })
            
            if resource_inserts:
                supabase.table('resources').insert(resource_inserts).execute()
                print(f"✅ Vaulted {len(resource_inserts)} unique resources.")

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