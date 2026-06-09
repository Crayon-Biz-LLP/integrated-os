import re

with open('core/skills/backfill_graph.py', 'r') as f:
    content = f.read()

def remove_func(text, func_name):
    pattern = r"def " + func_name + r"\(.*?(?=\n#|\ndef |\Z)"
    return re.sub(pattern, "", text, flags=re.DOTALL)

content = remove_func(content, "gemini_with_retry_sync")

with open('core/skills/backfill_graph.py', 'w') as f:
    f.write(content)
