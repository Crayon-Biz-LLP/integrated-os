#!/usr/bin/env python3
"""
Convert error/warning print() calls to audit_log() calls.
Focuses on prints containing ⚠️, ❌, Error, Warning, etc.
"""
import re
import sys

def convert_file(filepath):
    with open(filepath, 'r') as f:
        lines = f.readlines()
    
    # Determine service name
    if 'pulse.py' in filepath:
        service = 'pulse'
    elif 'webhook.py' in filepath:
        service = 'webhook'
    elif 'backfill_graph.py' in filepath:
        service = 'backfill_graph'
    else:
        service = 'unknown'
    
    new_lines = []
    changes = 0
    
    for line in lines:
        # Match print statements that contain error/warning indicators
        # Pattern: print(f"...⚠️...") or print(f"...❌...") or print(f"...Error...")
        match = re.match(r'^(\s*)print\(f["\'](.*?)(⚠️|❌|Error|error|Warning|warning|Critical)(.*?)["\']\)\s*$', line, re.IGNORECASE)
        
        if match:
            indent = match.group(1)
            prefix = match.group(2)
            indicator = match.group(3)
            suffix = match.group(4)
            
            # Determine log level
            if '⚠️' in indicator or 'warning' in indicator.lower():
                level = 'WARNING'
            elif '❌' in indicator or 'error' in indicator.lower():
                level = 'ERROR'
            elif 'Critical' in indicator or 'CRITICAL' in indicator:
                level = 'CRITICAL'
            else:
                level = 'INFO'
            
            # Build the message (combine prefix + indicator + suffix)
            message = prefix + indicator + suffix
            # Remove f-string braces for simplicity in conversion
            # We'll pass the whole f-string as message
            original_fstring = prefix + indicator + suffix
            
            # Create audit_log call
            # We need to preserve the f-string, so we keep it as a regular string
            # Actually, we can't convert f-strings easily. Let's use audit_log_sync for simplicity
            new_line = f"{indent}audit_log_sync(\"{service}\", \"{level}\", f\"{original_fstring}\")\n"
            new_lines.append(new_line)
            changes += 1
        else:
            new_lines.append(line)
    
    if changes > 0:
        with open(filepath, 'w') as f:
            f.writelines(new_lines)
        print(f"✅ Converted {changes} print() calls in {filepath}")
        return True
    else:
        print(f"⚠️ No matching print() calls found in {filepath}")
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
