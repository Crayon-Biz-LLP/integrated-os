with open("core/pulse/context.py", "r") as f:
    content = f.read()

# Add get_people and get_recently_completed_tasks to the ContextProvider class
old_caches = """        self.caches = {
            'tasks': SimpleCache(ttl_seconds=30),
            'projects': SimpleCache(ttl_seconds=300),
            'people': SimpleCache(ttl_seconds=300),
            'calendar': SimpleCache(ttl_seconds=300)
        }"""

new_caches = """        self.caches = {
            'tasks': SimpleCache(ttl_seconds=30),
            'projects': SimpleCache(ttl_seconds=300),
            'people': SimpleCache(ttl_seconds=300),
            'calendar': SimpleCache(ttl_seconds=300),
            'recent_tasks': SimpleCache(ttl_seconds=60)
        }"""

content = content.replace(old_caches, new_caches)

# Add new methods before hydrate_tasks_context
insertion_point = "    async def hydrate_tasks_context(self, query_text: str = None, max_chars: int = 4000):"

new_methods = """    async def get_people(self):
        cached = self.caches['people'].get()
        if cached is not None:
            return cached
            
        res = supabase.table('people').select('id, name, strategic_weight').execute()
        people = res.data or []
        self.caches['people'].set(people)
        return people
        
    async def get_recently_completed_tasks(self, hours: int = 24):
        cached = self.caches['recent_tasks'].get()
        if cached is not None:
            return cached
            
        since_utc = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = supabase.table('tasks') \\
            .select('title, project_id, updated_at') \\
            .eq('is_current', False) \\
            .eq('status', 'done') \\
            .gte('updated_at', since_utc) \\
            .order('updated_at', desc=True) \\
            .limit(10) \\
            .execute()
            
        completed = res.data or []
        self.caches['recent_tasks'].set(completed)
        return completed

    async def get_calendar_context_formatted(self, target_date):
        events = await self.get_calendar_events(target_date)
        if not events:
            return "None"
            
        lines = []
        for e in events:
            try:
                t = e["time"][:16].replace("T", " ")
                src = "Google" if e.get("source") == "google" else "Outlook"
                lines.append(f"- {t} - {e['title']} ({src})")
            except Exception:
                lines.append(f"- {e.get('title', 'Untitled')}")
        return "\\n".join(lines)

"""

content = content.replace(insertion_point, new_methods + insertion_point)

with open("core/pulse/context.py", "w") as f:
    f.write(content)
print("Added get_people, get_recently_completed_tasks, and get_calendar_context_formatted to ContextProvider")
