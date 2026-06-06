
with open("core/webhook/dispatch.py", "r") as f:
    content = f.read()

# 1. Imports
content = content.replace("from core.services.outlook_service import get_outlook_calendar_events\\n", "")
content = content.replace("get_google_creds, MemoryCache, ", "")
content = content.replace("from googleapiclient.discovery import build\\n", "")

# 2. Calendar blocks
old_cal = """        # Google Calendar events for target day
        try:
            service = build('calendar', 'v3', credentials=get_google_creds(), cache=MemoryCache())
            events_res = service.events().list(
                calendarId='primary',
                timeMin=target.isoformat(),
                timeMax=target_end.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            for e in events_res.get('items', []):
                start = e.get('start', {})
                dt = start.get('dateTime') or start.get('date', '')
                summary = e.get('summary', 'Untitled')
                events_list.append({"time": dt, "title": summary})
        except Exception as cal_err:
            audit_log_sync("webhook", "WARNING", f"Brief calendar query failed: {cal_err}")

        # Outlook calendar events for target day
        try:
            outlook_events = get_outlook_calendar_events(target)
            for e in outlook_events:
                events_list.append({"time": e["time"], "title": e["title"]})
        except Exception as ol_err:
            audit_log_sync("webhook", "WARNING", f"Brief Outlook calendar query failed: {ol_err}")"""

new_cal = """        # Unified Calendar events for target day
        try:
            cal_events = await context_provider.get_calendar_events(target)
            for e in cal_events:
                events_list.append({"time": e.get("time", ""), "title": e.get("title", "")})
        except Exception as cal_err:
            audit_log_sync("webhook", "WARNING", f"Brief calendar query failed: {cal_err}")"""

content = content.replace(old_cal, new_cal)

# 3. Tasks block
old_tasks = """        # Recent completions
        try:
            comp_res = supabase.table('tasks') \\
                .select('title, project_id') \\
                .eq('is_current', False) \\
                .eq('status', 'done') \\
                .gte('updated_at', since_utc) \\
                .order('updated_at', desc=True) \\
                .limit(5) \\
                .execute()
            completed_raw = comp_res.data or []
            if completed_raw:
                done_proj_ids = list(set(t.get('project_id') for t in completed_raw if t.get('project_id')))
                done_proj_map = {}
                if done_proj_ids:
                    done_proj_res = supabase.table('projects').select('id, name').in_('id', done_proj_ids).execute()
                    for p in (done_proj_res.data or []):
                        done_proj_map[p['id']] = p['name']
                for t in completed_raw:
                    pn = done_proj_map.get(t.get('project_id'), 'INBOX')
                    recently_completed.append(_format_task_line(t['title'], pn))
        except Exception:
            pass"""

new_tasks = """        # Recent completions
        try:
            completed_raw = await context_provider.get_recently_completed_tasks()
            if completed_raw:
                projects = await context_provider.get_projects()
                proj_map = {p['id']: p['name'] for p in projects}
                for t in completed_raw:
                    pn = proj_map.get(t.get('project_id'), 'INBOX')
                    recently_completed.append(_format_task_line(t.get('title', ''), pn))
        except Exception as err:
            audit_log_sync("webhook", "WARNING", f"Brief recent completions failed: {err}")"""

content = content.replace(old_tasks, new_tasks)

with open("core/webhook/dispatch.py", "w") as f:
    f.write(content)
print("Patched dispatch.py")
