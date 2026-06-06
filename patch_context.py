import re

with open("core/pulse/context.py", "r") as f:
    content = f.read()

# Replace the broken end
old_end = """        return compressed_tasks, universal[:4000]

# Global instance
context_provider = ContextProvider()

    async def hydrate_memories_context(self, query_text: str, match_count: int = 5):
        \"\"\"Uses pgvector to find semantically relevant memories.\"\"\"
        if not query_text:
            return "None"
            
        try:
            embedding = await asyncio.to_thread(get_embedding, query_text)
            if not embedding:
                return "None"
                
            res = supabase.rpc('match_memories', {
                'query_embedding': embedding,
                'match_count': match_count,
                'match_threshold': 0.6
            }).execute()
            
            memories = res.data or []
            if not memories:
                return "None"
                
            lines = []
            for m in memories:
                lines.append(f"[{m.get('memory_type', 'note').upper()}] {m.get('content')}")
            return "\\n".join(lines)
            
        except Exception as e:
            print(f"Memory hydration failed: {e}")
            return "None"
"""

new_end = """        return compressed_tasks, universal[:4000]

    async def hydrate_memories_context(self, query_text: str, match_count: int = 5):
        \"\"\"Uses pgvector to find semantically relevant memories.\"\"\"
        if not query_text:
            return "None"
            
        try:
            embedding = await asyncio.to_thread(get_embedding, query_text)
            if not embedding:
                return "None"
                
            res = supabase.rpc('match_memories', {
                'query_embedding': embedding,
                'match_count': match_count,
                'match_threshold': 0.6
            }).execute()
            
            memories = res.data or []
            if not memories:
                return "None"
                
            lines = []
            for m in memories:
                lines.append(f"[{m.get('memory_type', 'note').upper()}] {m.get('content')}")
            return "\\n".join(lines)
            
        except Exception as e:
            print(f"Memory hydration failed: {e}")
            return "None"

# Global instance
context_provider = ContextProvider()
"""

if old_end in content:
    content = content.replace(old_end, new_end)
    with open("core/pulse/context.py", "w") as f:
        f.write(content)
    print("Fixed context.py")
else:
    print("Could not find broken end block")
