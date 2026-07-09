#!/usr/bin/env python3
"""Fail if any graph_nodes insert/upsert is missing normalized_label.

Usage:
    python3 scripts/check_graph_nodes_normalized_label.py

Exit code 1 if any violation found. Excludes tests/ and scripts/archive/.
"""
import os
import re
import sys

EXCLUDE_DIRS = {'tests', 'scripts/archive', 'node_modules', '__pycache__', '.git'}
RE_WRITE = re.compile(
    r"""table\(['"]graph_nodes['"]\)\.(?:insert|upsert)\("""
)

def check_file(path: str) -> list[str]:
    with open(path) as f:
        lines = f.readlines()

    issues = []
    for i, line in enumerate(lines):
        m = RE_WRITE.search(line)
        if not m:
            continue
        # Check the next 15 lines for normalized_label
        found = any('normalized_label' in lines[j] for j in range(i, min(i + 15, len(lines))))
        if not found:
            issues.append(f"  {path}:{i+1}  missing normalized_label")
    return issues

def main():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    all_issues = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded dirs
        rel = os.path.relpath(dirpath, root)
        parts = rel.split(os.sep)
        if any(part in EXCLUDE_DIRS for part in parts):
            dirnames[:] = []
            continue

        for fn in filenames:
            if fn.endswith('.py'):
                fpath = os.path.join(dirpath, fn)
                issues = check_file(fpath)
                all_issues.extend(issues)

    if all_issues:
        print("ERROR: graph_nodes write without normalized_label:")
        for issue in all_issues:
            print(issue)
        sys.exit(1)

    print("OK: all graph_nodes writes include normalized_label")

if __name__ == '__main__':
    main()
