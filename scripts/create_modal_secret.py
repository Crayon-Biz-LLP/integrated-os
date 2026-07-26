#!/usr/bin/env python3
"""Reads the project .env file and creates a Modal secret with all env vars.

Usage:
    python scripts/create_modal_secret.py

This script:
1. Reads .env (or .env.local as fallback) in the project root
2. Filters out frontend-only vars and empty values
3. Builds the 'modal secret create' command
4. Prints the command for you to run (or runs it if --execute is passed)
"""

import os
import sys
import subprocess

# Keys to EXCLUDE — these are frontend-only or internal
EXCLUDE_KEYS = {
    "NEXT_PUBLIC_SUPABASE_URL",
    "NEXT_PUBLIC_SUPABASE_ANON_KEY",
    "NEXT_PUBLIC_API_URL",
}

# Map legacy keys to new names if needed (none for now)
KEY_MAP = {}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env_file(path):
    """Parse a simple KEY=VALUE env file."""
    env_vars = {}
    if not os.path.exists(path):
        return env_vars

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if value:
                env_vars[key] = value
    return env_vars


def main():
    # Try .env first, then .env.local
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(env_path):
        env_path = os.path.join(PROJECT_ROOT, ".env.local")
        if not os.path.exists(env_path):
            print("❌ No .env or .env.local found in project root.")
            sys.exit(1)

    env_vars = load_env_file(env_path)

    # Apply key mappings
    for old_key, new_key in KEY_MAP.items():
        if old_key in env_vars:
            env_vars[new_key] = env_vars.pop(old_key)

    # Build the modal secret create command
    cmd_parts = ["modal", "secret", "create", "rhodey-os"]
    added = 0
    skipped = 0

    for key, value in sorted(env_vars.items()):
        if key in EXCLUDE_KEYS:
            skipped += 1
            continue
        if not value or value == "":
            skipped += 1
            continue
        # Escape single quotes in values
        escaped_value = value.replace("'", "'\\''")
        cmd_parts.append(f"{key}='{escaped_value}'")
        added += 1

    print(f"📄 Loaded {len(env_vars)} vars from {env_path}")
    print(f"✅ {added} vars will be added to Modal secret 'rhodey-os'")
    print(f"⏭️  {skipped} vars excluded (frontend-only or empty)")
    print()

    if "--execute" in sys.argv:
        print("🚀 Running modal secret create...")
        # Use args list (no shell=True) to prevent shell injection
        # Each key=value pair is passed as a separate argument
        args_list = ["modal", "secret", "create", "rhodey-os"]
        for key, value in sorted(env_vars.items()):
            if key in EXCLUDE_KEYS:
                continue
            if not value or value == "":
                continue
            args_list.append(f"{key}={value}")
        result = subprocess.run(args_list, capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ Modal secret 'rhodey-os' created successfully!")
        else:
            print(f"❌ Failed: {result.stderr}")
            sys.exit(1)
    else:
        print("Run this command to create the secret:")
        print()
        print("  modal secret create rhodey-os \\")
        for key, value in sorted(env_vars.items()):
            if key in EXCLUDE_KEYS:
                continue
            if not value or value == "":
                continue
            # Truncate display for long values
            display = value[:60] + "..." if len(value) > 60 else value
            print(f"    {key}={display}")
        print()
        print("Or run with --execute to do it automatically (shell-safe):")
        print(f"  python {sys.argv[0]} --execute")


if __name__ == "__main__":
    main()
