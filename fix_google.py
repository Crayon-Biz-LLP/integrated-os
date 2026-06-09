import re

with open('core/services/google_service.py', 'r') as f:
    content = f.read()

def remove_func(text, func_name):
    pattern = r"def " + func_name + r"\(.*?(?=\ndef |\Z)"
    return re.sub(pattern, "", text, flags=re.DOTALL)

for func in ["get_google_calendar_events_range", "get_calendar_context"]:
    content = remove_func(content, func)

with open('core/services/google_service.py', 'w') as f:
    f.write(content)
