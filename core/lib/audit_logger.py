"""
Audit Logger - Replaces print() with permanent audit trail.
Writes to Supabase audit_logs table for observability.
"""
import json
import contextvars
import traceback
from core.services.db import get_supabase

# D3: Context variable for request-level tracing
trace_id_var = contextvars.ContextVar('trace_id', default=None)

def set_trace_id(trace_id: str = None) -> str:
    """D3: Set or generate a trace_id for the current request context.
    Returns the trace_id. Use at every entry point (webhook, pulse, API)."""
    import uuid
    if not trace_id:
        trace_id = str(uuid.uuid4())[:12]  # Short 12-char trace_id for readability
    trace_id_var.set(trace_id)
    return trace_id


def get_trace_id() -> str:
    """D3: Get the current trace_id or empty string."""
    tid = trace_id_var.get()
    return tid or ""

try:
    supabase = get_supabase()
except Exception:
    supabase = None

async def audit_log(service: str, level: str, message: str, metadata: dict = None):
    """
    Write an audit log entry to Supabase audit_logs table.
    
    Args:
        service: 'pulse', 'webhook', 'backfill_graph', etc.
        level: 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
        message: Log message (truncated to 500 chars)
        metadata: Additional context (error stack, memory_id, etc.)
    """
    try:
        if not supabase:
            return
            
        meta = metadata or {}
        tid = trace_id_var.get()
        if tid:
            meta['trace_id'] = tid
            
        log_data = {
            "service": service,
            "level": level,
            "message": message[:500] if message else "(empty)",
            "metadata": json.dumps(meta)
        }
        supabase.table('audit_logs').insert(log_data).execute()
    except Exception as e:
        # Fallback to print if audit_logs write fails
        print(f"⚠️ AUDIT LOG FAILURE: {e} | Original: [{service}] {level}: {message}")


def audit_log_sync(service: str, level: str, message: str, metadata: dict = None):
    """
    Synchronous version of audit_log.
    Use in non-async contexts (e.g., webhook.py).
    """
    try:
        if not supabase:
            return
            
        meta = metadata or {}
        tid = trace_id_var.get()
        if tid:
            meta['trace_id'] = tid
            
        log_data = {
            "service": service,
            "level": level,
            "message": message[:500] if message else "(empty)",
            "metadata": json.dumps(meta)
        }
        supabase.table('audit_logs').insert(log_data).execute()
    except Exception as e:
        print(f"⚠️ AUDIT LOG FAILURE: {e} | Original: [{service}] {level}: {message}")


def format_error(e: Exception) -> dict:
    """Format an exception into metadata dict."""
    return {
        "error_type": type(e).__name__,
        "error_message": str(e)[:200],
        "traceback": traceback.format_exc()[:500] if hasattr(traceback, 'format_exc') else None
    }


# Convenience wrappers
def info(service: str, message: str, metadata: dict = None):
    """Log INFO level."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(audit_log(service, 'INFO', message, metadata))
        else:
            audit_log_sync(service, 'INFO', message, metadata)
    except Exception:
        audit_log_sync(service, 'INFO', message, metadata)


def warning(service: str, message: str, metadata: dict = None):
    """Log WARNING level."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(audit_log(service, 'WARNING', message, metadata))
        else:
            audit_log_sync(service, 'WARNING', message, metadata)
    except Exception:
        audit_log_sync(service, 'WARNING', message, metadata)


def error(service: str, message: str, metadata: dict = None):
    """Log ERROR level."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(audit_log(service, 'ERROR', message, metadata))
        else:
            audit_log_sync(service, 'ERROR', message, metadata)
    except Exception:
        audit_log_sync(service, 'ERROR', message, metadata)


def critical(service: str, message: str, metadata: dict = None):
    """Log CRITICAL level."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(audit_log(service, 'CRITICAL', message, metadata))
        else:
            audit_log_sync(service, 'CRITICAL', message, metadata)
    except Exception:
        audit_log_sync(service, 'CRITICAL', message, metadata)

def log_audit(function_name: str, event_type: str, message: str, raw_input=None):
    """
    T-004 implementation: Writes to system_audit_logs.
    """
    try:
        if not supabase:
            return
        
        log_data = {
            "function_name": function_name,
            "event_type": event_type,
            "message": str(message)[:1000] if message else None,
            "raw_input": str(raw_input)[:1000] if raw_input else None
        }
        supabase.table("system_audit_logs").insert(log_data).execute()
    except Exception as e:
        print(f"⚠️ SYSTEM AUDIT LOG FAILURE: {e} | {function_name} | {message}")

def write_dlq(source_table: str, source_id: str, content: str, failure_reason: str):
    """
    T-003 implementation: Writes to dead_letter_queue.
    """
    try:
        if not supabase:
            return
            
        dlq_data = {
            "source_table": source_table,
            "source_id": str(source_id) if source_id else None,
            "content": str(content)[:2000] if content else None,
            "failure_reason": str(failure_reason)[:1000] if failure_reason else None
        }
        supabase.table("dead_letter_queue").insert(dlq_data).execute()
        
        # Also log to audit_logs
        log_audit("write_dlq", "dlq_write", f"DLQ entry created for {source_table} {source_id}", raw_input=failure_reason)
    except Exception as e:
        print(f"⚠️ DLQ WRITE FAILURE: {e} | {source_table} | {failure_reason}")
