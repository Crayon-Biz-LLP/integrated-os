"""
Single URL quarantine filter for all ingress points.

A URL-bearing message is NEVER a memory, a task, or a graph entity.
URLs are resources only. This module is the single source of truth
for that quarantine decision — call it at the top of every channel handler.

Usage:
    result = check_and_quarantine_url(text, source="telegram")
    if result.is_url:
        return  # URL routed to resources, pipeline ends
"""

import re
from typing import Optional
from dataclasses import dataclass
from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync


@dataclass
class URLQuarantineResult:
    """Result of a URL quarantine check."""
    is_url: bool = False
    url: Optional[str] = None
    action: str = "none"  # 'inserted', 'dismissed', 'skipped_dedup', 'none'
    message: str = ""


# Compiled pattern for URL extraction
_URL_PATTERN = re.compile(r'https?://\S+', re.IGNORECASE)

# Characters to strip from the end of extracted URLs
_URL_TRAILING_CHARS = '.,;:!?)\'"' + '\\'


def extract_url(text: str) -> Optional[str]:
    """Extract the first URL from text. Returns cleaned URL or None."""
    match = _URL_PATTERN.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(_URL_TRAILING_CHARS).rstrip('.')
    return url


def check_and_quarantine_url(
    text: str,
    source: str = "unknown",
    skip_insert: bool = False,
) -> URLQuarantineResult:
    """Primary ingress filter for URL-bearing content.

    Call this at the top of every channel handler.
    If ``is_url`` is True, the caller MUST skip all downstream processing
    (classification, entity extraction, memory/task creation).

    Args:
        text: Raw incoming text
        source: Source label for audit logs
        skip_insert: If True, only check — don't actually insert

    Returns:
        URLQuarantineResult with is_url indicating URL presence
    """
    url = extract_url(text)
    if not url:
        return URLQuarantineResult()

    if skip_insert:
        return URLQuarantineResult(
            is_url=True,
            url=url,
            action="detected",
            message=f"URL detected: {url[:80]}"
        )

    supabase = get_supabase()

    try:
        existing = supabase.table('resources') \
            .select('id, dismissed_at') \
            .eq('url', url) \
            .limit(1) \
            .execute()
    except Exception as e:
        audit_log_sync("url_filter", "WARNING", f"Resource lookup failed: {e}")
        existing = type('obj', (object,), {'data': None})()

    if existing.data:
        row = existing.data[0]
        if row.get('dismissed_at'):
            audit_log_sync("url_filter", "INFO",
                           f"Skipped dismissed URL: {url[:80]}")
            return URLQuarantineResult(
                is_url=True,
                url=url,
                action="dismissed",
                message="Already seen this link and dismissed it. Skipping."
            )
        return URLQuarantineResult(
            is_url=True,
            url=url,
            action="skipped_dedup",
            message="URL already exists in resources."
        )

    try:
        supabase.table('resources').insert({
            "url": url,
            "source": source,
        }).execute()
        audit_log_sync("url_filter", "INFO", f"Inserted resource: {url[:80]}")
        return URLQuarantineResult(
            is_url=True,
            url=url,
            action="inserted",
            message="URL logged as resource."
        )
    except Exception as e:
        audit_log_sync("url_filter", "WARNING", f"Resource insert failed: {e}")
        return URLQuarantineResult(
            is_url=True,
            url=url,
            action="error",
            message=f"Resource insert failed: {e}"
        )


def is_url_text(text: str) -> bool:
    """
    Fast check: does text contain a URL? No DB calls, no inserts.
    Use for early-return guards in hot paths.
    """
    return bool(_URL_PATTERN.search(text))
