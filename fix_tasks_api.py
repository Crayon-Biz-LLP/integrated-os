import re

with open('frontend/src/lib/tasks/api.ts', 'r') as f:
    content = f.read()

def remove_func(text, func_name):
    pattern = r"export async function " + func_name + r"\(.*?(?=\nexport async function |\Z)"
    return re.sub(pattern, "", text, flags=re.DOTALL)

for func in ["fetchTasks", "fetchTaskStats", "updateTaskStatus"]:
    content = remove_func(content, func)

# Clean up imports
content = content.replace("TaskFilters, TaskStats, ", "")

with open('frontend/src/lib/tasks/api.ts', 'w') as f:
    f.write(content)
