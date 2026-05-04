# api/webhook.py
import os
import json
import asyncio
import httpx
import re
import base64
from email.mime.text import MIMEText
from supabase import create_client, Client
from datetime import datetime, timezone, timedelta
from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.discovery_cache import base

# Import versioned_update from pulse (with robust path handling for Vercel)
try:
    # Try direct import (works when both files are in same directory)
    from pulse import versioned_update
except ImportError:
    # Fallback: add parent directory to path
    import sys
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from pulse import versioned_update

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), 
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

def normalize_title(title: str) -> str:
    """Normalize title for comparison: lowercase, strip punctuation, collapse whitespace."""
    import re
    # Lowercase
    normalized = title.lower()
    # Strip punctuation (keep alphanumeric and spaces)
    normalized = re.sub(r'[^a-z0-9\s]', '', normalized)
    # Collapse repeated whitespace
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized

# ... rest of file continues
