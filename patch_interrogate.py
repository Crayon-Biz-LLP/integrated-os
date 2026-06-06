with open("core/webhook/dispatch.py", "r") as f:
    content = f.read()

if "from core.pulse.context import context_provider" not in content:
    content = content.replace("from core.lib.conversation", "from core.pulse.context import context_provider\nfrom core.lib.conversation")

# We need to replace the entire context building section in interrogate_brain
old_brain_context = """        embedding = await asyncio.to_thread(get_embedding, query)

        memories_res = supabase.rpc(
            'match_memories',
            {
                'query_embedding': embedding,
                'match_count': 5,
                'match_threshold': 0.5
            }
        ).execute()
        memories = memories_res.data if memories_res.data else []

        # TODO: If match_canonical_pages RPC does not exist yet in Supabase,
        # create it mirroring the match_memories pattern for canonical_pages table.
        combined_results = []
        for m in (memories or []):
            combined_results.append({
                "content": m.get('content', ''),
                "source": m.get('memory_type', 'memory').upper(),
                "link": m.get('url') or '',
                "similarity": m.get('similarity', 0)
            })

        try:
            canonical_res = supabase.rpc('match_canonical_pages', {
                'query_embedding': embedding,
                'match_count': 3,
                'match_threshold': 0.65
            }).execute()
            canonical_hits = canonical_res.data or []
            for hit in canonical_hits:
                combined_results.append({
                    "content": f"[CANONICAL] {hit.get('title', '')}: {hit.get('content', '')[:300]}",
                    "source": "CANONICAL",
                    "link": '',
                    "similarity": hit.get('similarity', 0)
                })
        except Exception as canon_err:
            print(f"Canonical pages search failed (RPC may not exist): {canon_err}")

        # Sort by similarity descending
        combined_results.sort(key=lambda x: x.get('similarity', 0), reverse=True)

        try:
            resources_res = supabase.table('resources').select('title, url, category, summary').execute()
            resources = resources_res.data or []
        except Exception:
            resources = []

        # Fetch active tasks with project names
        active_tasks_list = []
        raw_tasks = []
        proj_map = {}
        try:
            tasks_res = supabase.table('tasks').select('id, title, priority, project_id, status, reminder_at, created_at').eq('is_current', True).not_.in_('status', ['done', 'cancelled']).order('priority', desc=True).order('created_at', desc=True).execute()
            raw_tasks = tasks_res.data or []
            if raw_tasks:
                proj_ids = list(set(t.get('project_id') for t in raw_tasks if t.get('project_id')))
                proj_map = {}
                if proj_ids:
                    proj_res = supabase.table('projects').select('id, name, org_tag').in_('id', proj_ids).execute()
                    for p in (proj_res.data or []):
                        proj_map[p['id']] = p['name']
                for t in raw_tasks:
                    p_name = proj_map.get(t.get('project_id'), 'INBOX')
                    active_tasks_list.append(_format_task_line(t.get('title', ''), p_name, t.get('priority', 'todo')))
        except Exception as tasks_err:
            print(f"Active tasks query failed: {tasks_err}")

        # Overdue detection — tasks past their reminder_at
        overdue_tasks = []
        now_utc = datetime.now(timezone.utc).isoformat()
        for t in raw_tasks:
            reminder = t.get('reminder_at')
            if reminder and reminder < now_utc:
                p_name = proj_map.get(t.get('project_id'), 'INBOX')
                overdue_tasks.append(_format_task_line(t.get('title', ''), p_name))

        # Recent completions — tasks done in last 24h
        recently_completed = []
        try:
            since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            completed_res = supabase.table('tasks').select('title, priority, project_id, updated_at').eq('is_current', False).eq('status', 'done').gte('updated_at', since).order('updated_at', desc=True).limit(5).execute()
            completed_raw = completed_res.data or []
            if completed_raw:
                done_proj_ids = list(set(t.get('project_id') for t in completed_raw if t.get('project_id')))
                done_proj_map = {}
                if done_proj_ids:
                    done_proj_res = supabase.table('projects').select('id, name').in_('id', done_proj_ids).execute()
                    for p in (done_proj_res.data or []):
                        done_proj_map[p['id']] = p['name']
                for t in completed_raw:
                    p_name = done_proj_map.get(t.get('project_id'), 'INBOX')
                    recently_completed.append(_format_task_line(t.get('title', ''), p_name))
        except Exception as done_err:
            print(f"Recent completions query failed: {done_err}")

        all_context = []

        if tactical_map:
            all_context.append(f"TACTICAL MAP:\\n{tactical_map}")

        if active_tasks_list:
            all_context.append("ACTIVE TASKS:\\n" + "\\n".join(f"- {t}" for t in active_tasks_list))

        if overdue_tasks:
            all_context.append("OVERDUE:\\n" + "\\n".join(f"- {t}" for t in overdue_tasks))

        if recently_completed:
            all_context.append("RECENTLY COMPLETED (24h):\\n" + "\\n".join(f"- {t}" for t in recently_completed))

        for item in combined_results:
            source = item.get('source', 'memory').upper()
            content = item.get('content', '')
            link = item.get('link', '')
            all_context.append(f"[{source}] {content}" + (f" | Link: {link}" if link else ""))

        for r in resources[:3]:
            title = r.get('title', 'Untitled')
            url = r.get('url', '')
            category = r.get('category', 'resource')
            summary = r.get('summary', title)
            all_context.append(f"[{category.upper()}] {summary}" + (f" | Link: {url}" if url else ""))"""

new_brain_context = """        compressed_tasks, _ = await context_provider.hydrate_tasks_context(query)
        memories_context = await context_provider.hydrate_memories_context(query)
        
        all_context = []
        if tactical_map:
            all_context.append(f"TACTICAL MAP:\\n{tactical_map}")
            
        all_context.append(f"ACTIVE TASKS:\\n{compressed_tasks}")
        
        if memories_context and memories_context != "None":
            all_context.append(f"RELEVANT MEMORIES:\\n{memories_context}")"""

if old_brain_context in content:
    content = content.replace(old_brain_context, new_brain_context)
    with open("core/webhook/dispatch.py", "w") as f:
        f.write(content)
    print("Patched dispatch.py interrogate_brain with ContextProvider")
else:
    print("Could not find old_brain_context block!")
