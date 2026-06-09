import re

with open('core/agents/cleanup_orphans.py', 'r') as f:
    content = f.read()

def remove_func(text, func_name):
    pattern = r"def " + func_name + r"\(.*?(?=\ndef |\Z)"
    return re.sub(pattern, "", text, flags=re.DOTALL)

content = remove_func(content, "cleanup_orphan_memories")

# Also remove the call to it in __main__
content = re.sub(r'print\("Memories:"\)\s+cleanup_orphan_memories\(dry_run\)\s+', '', content)

with open('core/agents/cleanup_orphans.py', 'w') as f:
    f.write(content)
