with open("core/pulse/engine.py", "r") as f:
    content = f.read()

old_mem = """        # 🧠 RECENT MEMORIES (semantic search based on today's tasks)
        recent_memories_context = await get_recent_memories_for_briefing(filtered_tasks)"""

new_mem = """        # 🧠 RECENT MEMORIES (Phase 2 semantic search)
        mem_query = " | ".join([t.get('title', '') for t in filtered_tasks[:5]])
        recent_memories_context = await context_provider.hydrate_memories_context(mem_query, match_count=5)"""

content = content.replace(old_mem, new_mem)

with open("core/pulse/engine.py", "w") as f:
    f.write(content)
print("Patched memories in engine.py")
