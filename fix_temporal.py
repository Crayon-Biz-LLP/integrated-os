import re

with open('core/lib/temporal_lineage.py', 'r') as f:
    content = f.read()

def remove_func(text, func_name):
    # Match from def func_name to the next def or end of file
    pattern = r"def " + func_name + r"\(.*?(?=\ndef |\Z)"
    return re.sub(pattern, "", text, flags=re.DOTALL)

for func in ["create_versioned_memory", "create_versioned_project", "get_memory_history", "get_state_at_time"]:
    content = remove_func(content, func)

with open('core/lib/temporal_lineage.py', 'w') as f:
    f.write(content)
