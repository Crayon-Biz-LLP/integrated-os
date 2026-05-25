with open('core/pulse/memory.py', 'r') as f:
    content = f.read()

import re

start_idx = content.find("async def serendipity_engine(")
end_idx = content.find("async def adaptive_briefing_learner(", start_idx)

original_func = content[start_idx:end_idx]

new_func = """async def serendipity_engine(active_tasks: list, people: list, resources: list) -> str:
    \"\"\"
    SERENDIPITY ENGINE: Surfaces unexpected multi-hop connections in the knowledge graph.
    Uses PostgreSQL Recursive CTEs to find hidden 2nd and 3rd degree links between today's tasks
    and historical projects, people, or resources.
    \"\"\"
    try:
        from core.pulse.llm import supabase
        import random
        
        # 1. Gather all active task IDs
        task_ids = [str(t.get('id')) for t in active_tasks if t.get('id')]
        if not task_ids:
            return "No active tasks to base serendipity queries on."
            
        # 2. Find the graph_node IDs for these tasks
        # Assuming metadata->>task_id is how task nodes are linked
        nodes_res = supabase.table('graph_nodes').select('id').in_('metadata->>task_id', task_ids).execute()
        start_node_ids = [n['id'] for n in nodes_res.data]
        
        if not start_node_ids:
            return "No graph nodes found for active tasks."
            
        # 3. Call the Supabase RPC
        rpc_res = supabase.rpc('find_serendipity_paths', {'start_node_ids': start_node_ids, 'max_depth': 3}).execute()
        paths = rpc_res.data
        
        if not paths:
            return "No multi-hop connections found in the graph."
            
        # 4. Sample up to 30 paths to prevent token bloat and guarantee novelty
        if len(paths) > 30:
            paths = random.sample(paths, 30)
            
        # 5. Format the paths beautifully for the LLM
        formatted_paths = []
        for path in paths:
            labels = path.get('path_labels', [])
            types = path.get('path_types', [])
            relations = path.get('path_relations', [])
            weight = path.get('total_weight', 0.0)
            
            # Reconstruct the string: Task [X] --RELATES_TO--> Person [Y]
            path_str_parts = []
            for i in range(len(labels)):
                if i == 0:
                    path_str_parts.append(f"{types[i].capitalize()} [{labels[i]}]")
                else:
                    path_str_parts.append(f"--{relations[i]}--> {types[i].capitalize()} [{labels[i]}]")
                    
            path_str = " ".join(path_str_parts)
            formatted_paths.append(f"- Path (Weight {weight}): {path_str}")
            
        final_output = "✨ HIDDEN GRAPH CONNECTIONS (MULTI-HOP):\\n" + "\\n".join(formatted_paths)
        return final_output

    except Exception as e:
        from core.lib.audit_logger import audit_log_sync
        audit_log_sync("pulse", "WARNING", f"⚠️ Serendipity Engine failed (non-critical): {e}")
        return ""

"""

if original_func:
    content = content.replace(original_func, new_func)
    with open('core/pulse/memory.py', 'w') as f:
        f.write(content)
    print("Patched serendipity_engine successfully.")
else:
    print("Failed to find serendipity_engine.")
