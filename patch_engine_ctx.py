with open("core/pulse/engine.py", "r") as f:
    content = f.read()

import_statement = "from core.pulse.context import context_provider"
if import_statement not in content:
    content = content.replace("from core.pulse.graph import", import_statement + "\nfrom core.pulse.graph import")

old_tasks_block = """        # Task-boundary-safe truncation: split on ' | ' delimiter and accumulate complete tasks
        parts = compressed_tasks.split(' | ')
        safe_parts = []
        running_len = 0
        for part in parts:
            if running_len + len(part) + 3 > 3000:
                break
            safe_parts.append(part)
            running_len += len(part) + 3
        compressed_tasks_final = ' | '.join(safe_parts)"""

new_tasks_block = """        # Phase 2: Context Hydration Engine
        query_focus = f"Briefing for {briefing_mode}"
        compressed_tasks_final, universal_task_map = await context_provider.hydrate_tasks_context(query_focus)"""

content = content.replace(old_tasks_block, new_tasks_block)

# Remove universal_task_map generation earlier
old_univ = """        # This is the AI's "Visual Field"
        universal_task_map = " | ".join([f"[ID:{t.get('id')}] {t.get('title')}" for t in recent_tasks])"""

new_univ = """        # Universal task map is now handled by context_provider"""

content = content.replace(old_univ, new_univ)

with open("core/pulse/engine.py", "w") as f:
    f.write(content)
print("Patched engine.py to use ContextProvider")
