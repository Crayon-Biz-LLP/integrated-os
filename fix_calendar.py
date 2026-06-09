import re

with open('frontend/src/lib/calendar/types.ts', 'r') as f:
    content = f.read()

def remove_interface(text, name):
    pattern = r"export interface " + name + r" \{.*?\}"
    return re.sub(pattern, "", text, flags=re.DOTALL)

content = remove_interface(content, "CalendarDay")
content = remove_interface(content, "CalendarStats")

with open('frontend/src/lib/calendar/types.ts', 'w') as f:
    f.write(content)
