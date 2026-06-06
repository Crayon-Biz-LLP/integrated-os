with open("core/webhook/dispatch.py", "r") as f:
    content = f.read()

# I will replace the task fetching part with ContextProvider
old_brief_tasks = """        # All active pending tasks
        try:
            tasks_res = supabase.table('tasks') \\
                .select('id, title, priority, project_id, status, reminder_at, created_at') \\
                .eq('is_current', True) \\
                .not_.in_('status', ['done', 'cancelled']) \\
                .order('priority', desc=True) \\
                .order('created_at', desc=True) \\
                .execute()
            raw_tasks = tasks_res.data or []
            if raw_tasks:
                proj_ids = list(set(t.get('project_id') for t in raw_tasks if t.get('project_id')))
                proj_map = {}
                if proj_ids:
                    proj_res = supabase.table('projects').select('id, name, org_tag').in_('id', proj_ids).execute()
                    for p in (proj_res.data or []):
                        proj_map[p['id']] = p['name']
                for t in raw_tasks:
                    pn = proj_map.get(t.get('project_id'), 'INBOX')
                    ts = t.get('reminder_at')
                    due = ""
                    if ts:
                        try:
                            due_dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                            if due_dt < target_end and due_dt >= target:
                                due = " 🔔 due today" if not day_offset else " 🔔 due tomorrow"
                        except Exception:
                            pass
                    active_tasks_list.append(_format_task_line(t['title'], pn, t.get('priority','todo'), due))
                    reminder = t.get('reminder_at')
                    if reminder and reminder < now_utc:
                        overdue_tasks.append(_format_task_line(t['title'], pn))
        except Exception as t_err:
            audit_log_sync("webhook", "WARNING", f"Brief tasks query failed: {t_err}")"""

new_brief_tasks = """        # All active pending tasks (via ContextProvider)
        try:
            compressed_tasks, _ = await context_provider.hydrate_tasks_context(text)
            active_tasks_list = compressed_tasks.split(" | ") if compressed_tasks else []
        except Exception as t_err:
            audit_log_sync("webhook", "WARNING", f"Brief tasks query failed: {t_err}")"""

if old_brief_tasks in content:
    content = content.replace(old_brief_tasks, new_brief_tasks)
    with open("core/webhook/dispatch.py", "w") as f:
        f.write(content)
    print("Patched dispatch.py handle_daily_brief with ContextProvider")
else:
    print("Could not find old_brief_tasks block!")
