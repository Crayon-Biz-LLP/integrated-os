
with open("core/pulse/engine.py", "r") as f:
    content = f.read()

# 1. Clean imports
content = content.replace("write_outcome_memory, get_recent_memories_for_briefing,", "write_outcome_memory,")
content = content.replace("get_calendar_context, check_conflict, sync_to_calendar,", "check_conflict, sync_to_calendar,")

# 2. Legacy projects
old_proj = """        print("📦 Step 2: Fetching projects...")
        projects_res = supabase.table('projects') \\
            .select('id, name, org_tag, description, parent_project_id, status, keywords') \\
            .eq('status', 'active') \\
            .execute()
        legacy_projects = projects_res.data or []"""

new_proj = """        print("📦 Step 2: Fetching projects...")
        legacy_projects = await context_provider.get_projects()"""
content = content.replace(old_proj, new_proj)

# 3. People (first occurrence)
old_people1 = """        print("📦 Step 3: Fetching people...")
        people_res = supabase.table('people').select('name, strategic_weight').execute()
        people = people_res.data or []"""

new_people1 = """        print("📦 Step 3: Fetching people...")
        people = await context_provider.get_people()"""
content = content.replace(old_people1, new_people1)

# 4. People (second occurrence)
old_people2 = """        # 🕸️ ADD-ON: Graph-aware person→task context (non-blocking)
        people_res = supabase.table('people').select('id, name').execute()
        people = people_res.data or []"""

new_people2 = """        # 🕸️ ADD-ON: Graph-aware person→task context (non-blocking)
        people = await context_provider.get_people()"""
content = content.replace(old_people2, new_people2)

# 5. Calendar
old_cal = """        # 📅 Fetch calendar context (Google + Outlook) for today
        target_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        calendar_context = get_calendar_context(target_day)"""

new_cal = """        # 📅 Fetch calendar context (Google + Outlook) for today
        target_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        calendar_context = await context_provider.get_calendar_context_formatted(target_day)"""
content = content.replace(old_cal, new_cal)

with open("core/pulse/engine.py", "w") as f:
    f.write(content)
print("Patched engine.py")
