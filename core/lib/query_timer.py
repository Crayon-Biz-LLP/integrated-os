"""
Lightweight per-query timer for diagnosing latency.

Usage:
    from core.lib.query_timer start_timer, mark, report

    start_timer("trace_id_123")
    # ... do work ...
    mark("trace_id_123", "classify_done")
    # ... more work ...
    mark("trace_id_123", "anaphora_done")
    # ... finish ...
    report("trace_id_123")  # prints to Modal stdout

Output in Modal logs:
    [PERF:trace_id_123] TOTAL=44.8s | core_config=0.5s | classify=4.2s | anaphora=3.8s | ...

Key design decisions:
- Zero HTTP calls: uses print() → Modal stdout, not audit_log_sync (which would add latency)
- Thread-safe: uses dict per trace_id, isolated per request
- Auto-cleanup: report() removes the timer entry
"""

import time
from typing import Dict, Optional

# In-memory timer store: trace_id -> {"start": float, "marks": {name: timestamp}}
_timers: Dict[str, dict] = {}


def start_timer(trace_id: str) -> None:
    """Start timing for a given trace_id."""
    _timers[trace_id] = {"start": time.time(), "marks": {}}


def mark(trace_id: str, name: str) -> None:
    """Record a timing checkpoint. Name should be short (e.g. 'classify', 'phase1a')."""
    timer = _timers.get(trace_id)
    if timer is not None:
        timer["marks"][name] = time.time()


def report(trace_id: str) -> Optional[str]:
    """Print timing summary to stdout and clean up. Returns the report string."""
    timer = _timers.pop(trace_id, None)
    if timer is None:
        return None

    total = time.time() - timer["start"]
    marks = timer["marks"]

    parts = []
    prev_time = timer["start"]
    for name, ts in marks.items():
        elapsed = ts - prev_time
        parts.append(f"{name}={elapsed:.1f}s")
        prev_time = ts

    report_str = f"[PERF:{trace_id}] TOTAL={total:.1f}s | {' | '.join(parts)}"
    print(report_str)
    return report_str
