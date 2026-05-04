#!/usr/bin/env python3
"""
Convert print() calls to audit_log() calls in pulse.py, webhook.py, backfill_graph.py.
Run: python3 core/convert_prints.py
"""
import re
import sys

def convert_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    original = content
    
    # Pattern 1: Simple print with f-string
    # print(f"something {var}") -> info("service", f"something {var}")
    pattern1 = r'print\(f["\'](.*?)["\']\)'
    
    # We need to be smarter about this - let's do line-by-line
    lines = content.split('\n')
    new_lines = []
    service = None
    
    # Determine service name from filepath
    if 'pulse.py' in filepath:
        service = 'pulse'
    elif 'webhook.py' in filepath:
        service = 'webhook'
    elif 'backfill_graph.py' in filepath:
        service = 'backfill_graph'
    else:
        service = 'unknown'
    
    for line in lines:
        stripped = line.strip()
        
        # Skip comments and empty lines
        if not stripped or stripped.startswith('#'):
            new_lines.append(line)
            continue
        
        # Check if line contains print(
        if 'print(' in line or 'print("' in line:
            # Get indentation
            indent = len(line) - len(line.lstrip())
            indent_str = ' ' * indent
            
            # Extract print content
            # Handle: print("message")
            # Handle: print(f"message {var}")
            # Handle: print(f"message: {e}")  <-- exception
            
            # Simple case: print("message") or print('message')
            simple_match = re.match(r'print\(["\'](.*?)["\']\)', stripped)
            fstring_match = re.match(r'print\(f["\'](.*?)["\']\)', stripped)
            
            if fstring_match or simple_match:
                msg = fstring_match.group(1) if fstring_match else simple_match.group(1)
                
                # Determine log level
                level = 'INFO'
                if '⚠️' in msg or '❌' in msg or '⚠' in msg:
                    level = 'WARNING'
                if 'Error' in msg or 'error' in msg or 'failed' in msg.lower():
                    level = 'ERROR'
                if 'Critical' in msg or 'CRITICAL' in msg:
                    level = 'CRITICAL'
                
                # Convert to audit_log call
                new_line = f'{indent_str}info("{service}", f"{msg}")'
                new_lines.append(new_line)
            else:
                # Complex print - wrap with info()
                # Replace print( with info("service", 
                new_line = re.sub(
                    r'print\(',
                    f'info("{service}", ',
                    line,
                    count=1
                )
                new_lines.append(new_line)
        else:
            new_lines.append(line)
    
    new_content = '\n'.join(new_lines)
    
    if new_content != original:
        with open(filepath, 'w') as f:
            f.write(new_content)
        print(f"✅ Converted {filepath}")
        return True
    else:
        print(f"⚠️ No changes in {filepath}")
        return False

if __name__ == '__main__':
    files = [
        'core/pulse.py',
        'core/webhook.py',
        'core/skills/backfill_graph.py'
    ]
    
    for f in files:
        try:
            convert_file(f)
        except Exception as e:
            print(f"❌ Error converting {f}: {e}")
    
    print("\n✅ Conversion complete. Review changes with: git diff")
