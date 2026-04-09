#!/usr/bin/env python3
"""
Pulse CLI - Command-line interface for running the pulse briefing.

Usage:
    python core/pulse_cli.py

Required env vars:
    PULSE_SECRET - Secret for authentication
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""
import os
import sys
import asyncio

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pulse import process_pulse


def main():
    print("Starting Pulse CLI...")
    
    pulse_secret = os.getenv("PULSE_SECRET")
    if not pulse_secret:
        print("ERROR: PULSE_SECRET not found in environment variables")
        sys.exit(1)
    
    print(f"Pulse secret found: {'*' * 20}")
    print("Running process_pulse...")
    
    try:
        result = asyncio.run(process_pulse(auth_secret=pulse_secret))
        
        if result.get("success"):
            print(f"✓ Pulse completed successfully")
            if result.get("briefing"):
                briefing_preview = result["briefing"][:100].replace("\n", " ")
                print(f"  Briefing preview: {briefing_preview}...")
            sys.exit(0)
        else:
            error = result.get("error", "Unknown error")
            print(f"✗ Pulse failed: {error}")
            sys.exit(1)
            
    except Exception as e:
        print(f"✗ Pulse crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()