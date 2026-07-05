# 33. Meta-Cognitive Learning Layer — Tier 5 of Rhodey's Intelligence

**Status:** Proposed (Jul 4, 2026)
**Session:** Part 16 (Danny pushed through 5 rounds of ideation to reach OS-level thinking)
**Previous attempts:** Rationale capture → Vowpal Wabbit → River ML → Counting table → Cognitive layer → OS-level

---

## 1. Problem Statement

Rhodey operates 4 intelligence tiers that process data, make decisions, and deliver insights — but **none of them improve with use.**

Every subsystem has implicit feedback signals that are discarded:

| Subsystem | Danny's Action | Current Behavior | What Gets Lost |
|---|---|---|---|
| Classification | Corrects NOTE→TASK via inline keyboard | Stores text pattern in `classifier_corrections` (3-word prefix only) | *Why* it was wrong: source, entity count, time of day, confidence score |
| Entity extraction | Rejects person node from Telegram (`graph.py:411`) | Marks `pending_graph_nodes.status='rejected'`, never analyzed | Source+type combo reliability: "Telegram person nodes without context = 90% reject" |
| Context retrieval | Asks follow-up vs. accepts answer from `interrogate_brain()` | No signal captured at all | Which source combos predict satisfaction |
| Briefing generation | Reads vs. ignores sections | No engagement tracking | Which formats, lengths, times work best |
| Task routing | Reassigns project via `/ed` or direct update | New project stored, correction pattern lost | Keyword→org→project mappings |
| Decision Pulse | Approves/rejects items in `process_decision_pulse()` (`engine.py:248-396`) | Item proceeds or sits, individual state lost | Per (object_type, source, has_context) approval rates |
| Email pipeline | Responds to, ignores, or creates task from email | No engagement tracking per sender | Sender trust scores, auto-classify patterns |
| Practices | Dismisses via `/drop-<practice>` (`handler.py:264-311`) | `metadata.status='dismissed'`, variants added to exclusion list | *Which types* of practices Danny ignores |
| Memory indexing | `schedule_index_memory()` (`pipeline.py:410-448`) fails silently | Job goes to `dead_letter` after 3 retries, no pattern analysis | *Which* memory types/contents fail to index |
| URL resource handling | `dispatch.py:665-676` vaults URL as resource | Insert or skip, done | *Which* URL patterns are noise vs. valuable |

**Daily cost:** Danny spends 30-45 minutes correcting the system — approving/rejecting, reassigning, reclassifying. Every interaction is a training signal. The system discards every one.

---

## 2. Root Cause Analysis

### 2a. Architectural Gap

The 4 tiers were designed as a **read-only intelligence layer** over a growing database:

```
Tier 1: Pulse Orchestration → Classify, route, execute, deliver
Tier 2: Context Hydration   → Fetch, cache, compress
Tier 3: Memory & Graph      → Retrieve, traverse, rank
Tier 4: Session Memory      → Anchor, signal, source-select
                              ↓
                    ALL OUTPUT → DELIVER → FORGET
```

There is no feedback loop from delivery back into configuration. No tier observes its own outcomes and adjusts.

### 2b. The Only Existing Feedback Is Text-Pattern Matching

`core/webhook/feedback_loop.py` (lines 1-100) captures classification overrides and injects them as text patterns. It:
- Parses `FEEDBACK_OVERRIDE` messages from `audit_logs` via regex (line 24): `r"FEEDBACK_OVERRIDE: user corrected '(\w+)' → '(\w+)'\s*\|\s*text='(.{0,80})'"`
- Extracts 3-word text patterns via `_extract_pattern()` (lines 35-43): takes first 3 meaningful words >3 chars
- Upserts into `classifier_corrections` table with count tracking
- Injects via `get_learned_corrections()` into the classify prompt as `LEARNED CORRECTIONS` section

**Limitations:**
1. **Only covers intent classification** — ignores entity extraction, context retrieval, task routing, decision pulse, email pipeline, practices
2. **Only learns text patterns** — never learns *why* a pattern was wrong (source, time of day, entity context, confidence)
3. **Only injects into prompts** — never adjusts confidence thresholds, never tunes subsystem behavior
4. **Max 50 corrections** with oldest-first eviction — loses rare but important patterns

### 2c. Seven+ Subsystems Have Zero Feedback

Every subsystem processes and forgets. The `decisions` table (`core/decisions.py`) records *what* was decided but not the feature context needed for pattern extraction. The `audit_logs` table was designed for debugging/tracing (`/why` command), not learning.

### 2d. No Cross-Dimensional Pattern Detection

A classification correction might correlate with an entity extraction failure from a specific source. A task routing correction might correlate with a recurring time-of-day pattern. The system never sees these correlations because each subsystem's feedback is isolated or non-existent.

---

## 3. What We Are Going to Do

Build a **Tier 5: Meta-Cognitive Learning Layer** — a distributed observation and self-tuning system spanning all subsystems.

### Design Principles (Karpathy-style)

1. **Observe before optimize** — you can't improve what you don't measure
2. **Count before model** — frequency tables beat ML libraries. Counting is inspectable, debuggable, and compound
3. **Every correction is training data** — Danny's 30-45 min/day is the most valuable signal in the system
4. **Tune existing knobs before adding new ones** — every subsystem already has thresholds, prompts, config
5. **Surface patterns, don't hide them** — Danny should read "what I learned this week" and override anything
6. **Local learning, global synthesis** — each subsystem learns locally. Weekly synthesis finds cross-system insights
7. **HITL-first, auto-pilot after proven confidence** — shadow mode first. Auto-apply after 50+ examples at 95%+

### Architecture

```
                    ┌──────────────────────────────────────────────────────┐
                    │           TIER 5: META-COGNITIVE LAYER               │
                    │                                                      │
 CLASSIFICATION ────▶│  ┌──────────┐    ┌──────────┐    ┌──────────────┐  │
 ENTITY EXTRACTION ─▶│  │Feedback  │───▶│ Local    │───▶│ Config       │──▶│──▶ INJECTED BACK
 CONTEXT RETRIEVAL ─▶│  │Sensor    │    │ Pattern  │    │ Injector     │  │    INTO SUBSYSTEM
 TASK ROUTING ───────▶│  │(telemetry│    │ Learner  │    │ (thresholds, │  │
 DECISION PULSE ─────▶│  │ INSERT)  │    │(counting)│    │  prompts)    │  │
 EMAIL PIPELINE ─────▶│  └──────────┘    └──────────┘    └──────────────┘  │
 PRACTICES ──────────▶│                                                      │
 ORG ROUTING ─────────▶│         │                                           │
 MEMORY INDEXING ─────▶│         ▼                                           │
                      │  ┌────────────────────────────────────┐             │
                      │  │ WEEKLY SYNTHESIS (Sentinel piggy-  │             │
                      │  │ back, Sundays only)                │             │
                      │  │ • extract_patterns() per subsystem │             │
                      │  │ • detect_drift() — compare weeks   │             │
                      │  │ • build_transparency_report()      │             │
                      │  └────────────┬───────────────────────┘             │
                      │               ▼                                      │
                      │  "What I Learned This Week" → Weekend Briefing       │
                      └──────────────────────────────────────────────────────┘
```

---

## 4. How We Are Going to Do It — Complete Implementation Plan

---

### Phase 1: Foundation + Instrumentation

**Goal:** Create telemetry infrastructure and instrument the 4 highest-signal subsystems in shadow mode.

**Duration:** ~2-3 sessions

---

#### Step 1.1 — Migration: `subsystem_telemetry` table

**File:** `db/21_subsystem_telemetry.sql` (NEW)

```sql
-- Tier 5 Meta-Cognitive Learning Layer
-- Stores structured observations from every subsystem

CREATE TABLE IF NOT EXISTS subsystem_telemetry (
    id              BIGSERIAL PRIMARY KEY,
    subsystem       TEXT NOT NULL,          -- 'classification', 'entity_extraction', etc.
    event_type      TEXT NOT NULL,          -- 'correction', 'approval', 'rejection', 'engagement', 'failure'
    features        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- structured context
    predicted       JSONB,                 -- what the system predicted/produced
    actual          JSONB,                 -- what actually happened / Danny chose
    outcome         TEXT NOT NULL,          -- 'correct', 'corrected', 'confirmed', 'rejected', 'ignored', 'failed'
    confidence      REAL,                  -- system's confidence if applicable
    latency_ms      INTEGER,               -- operation duration if applicable
    session_id      TEXT,                  -- links to conversation thread
    source          TEXT,                  -- 'webhook', 'pulse', 'sentinel', etc.
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telemetry_subsystem
    ON subsystem_telemetry(subsystem, outcome, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_telemetry_cleanup
    ON subsystem_telemetry(created_at);
```

**Also create the `subsystem_patterns` table:**

```sql
CREATE TABLE IF NOT EXISTS subsystem_patterns (
    id              SERIAL PRIMARY KEY,
    subsystem       TEXT NOT NULL,
    feature_hash    TEXT NOT NULL,          -- MD5 of key feature values
    feature_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    total_count     INTEGER DEFAULT 0,
    correct_count   INTEGER DEFAULT 0,
    corrected_count INTEGER DEFAULT 0,
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(subsystem, feature_hash)
);

CREATE INDEX IF NOT EXISTS idx_patterns_lookup
    ON subsystem_patterns(subsystem, feature_hash);
```

---

#### Step 1.2 — Module: `core/lib/telemetry.py` (NEW)

**Full implementation with every function:**

```python
"""
Tier 5 Meta-Cognitive Layer — Telemetry module.

Provides:
- emit_observation() — record a structured observation from any subsystem
- get_pattern_summary() — fetch patterns for a subsystem
- compute_pattern_confidence() — inference: given features, what's the predicted outcome?
- hash_features() — deterministic feature hashing for pattern grouping
"""

import hashlib
import json
from typing import Any, Optional
from datetime import datetime, timezone, timedelta

from core.services.db import get_supabase
from core.lib.audit_logger import audit_log_sync

# Minimum observations required before a pattern is considered meaningful
MIN_PATTERN_OBSERVATIONS = 3

# Confidence thresholds for auto-apply
CONFIDENCE_AUTO_APPLY = 0.90
CONFIDENCE_SUGGEST = 0.70
CONFIDENCE_REVIEW = 0.0


def hash_features(features: dict, subsystem: str) -> str:
    """
    Deterministic hash of key features for pattern grouping.
    
    Canonicalizes by sorting keys and only including non-null values.
    
    Args:
        features: Feature dict, e.g. {"source": "telegram", "node_type": "person"}
        subsystem: Subsystem name for namespacing
    
    Returns:
        First 16 chars of MD5 hexdigest
    """
    # Canonicalize: sort keys, include only non-null values
    canonical = {k: v for k, v in sorted(features.items()) if v is not None}
    raw = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.md5(f"{subsystem}:{raw}".encode()).hexdigest()[:16]


async def emit_observation(
    subsystem: str,
    event_type: str,
    features: dict,
    predicted: Any = None,
    actual: Any = None,
    outcome: str = "correct",
    confidence: Optional[float] = None,
    latency_ms: Optional[int] = None,
    session_id: Optional[str] = None,
    source: str = "webhook",
) -> bool:
    """
    Record a structured observation from any subsystem.
    
    This is the primary telemetry ingestion point. Every subsystem calls this
    when Danny provides feedback (correction, approval, rejection, engagement).
    
    Args:
        subsystem: 'classification', 'entity_extraction', 'context_retrieval',
                   'task_routing', 'decision_pulse', 'email_pipeline', 'practices'
        event_type: 'correction', 'approval', 'rejection', 'engagement', 'failure'
        features: Dict of structured feature values for pattern extraction.
                 Must include enough context to distinguish different scenarios.
        predicted: What the system originally produced (intent string, action string, etc.)
        actual: What actually happened (corrected intent, actual action, etc.)
        outcome: 'correct' (system was right), 'corrected' (Danny changed it),
                 'confirmed' (Danny agreed), 'rejected' (Danny said no),
                 'ignored' (Danny didn't act), 'failed' (system error)
        confidence: System's confidence score (0.0-1.0) if applicable
        latency_ms: How long the operation took
        session_id: Links to conversation thread for context
        source: 'webhook', 'pulse', 'sentinel', etc.
    
    Returns:
        True if observation was recorded, False on failure (fail-open)
    """
    try:
        supabase = get_supabase()
        supabase.table("subsystem_telemetry").insert({
            "subsystem": subsystem,
            "event_type": event_type,
            "features": features,
            "predicted": json.dumps(predicted) if predicted is not None else None,
            "actual": json.dumps(actual) if actual is not None else None,
            "outcome": outcome,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "session_id": session_id,
            "source": source,
        }).execute()
        
        # Also upsert the pattern counter
        await _update_pattern_count(subsystem, features, outcome)
        
        return True
    except Exception as e:
        # Fail-open: telemetry should never crash the calling subsystem
        audit_log_sync("telemetry", "WARNING", f"emit_observation failed: {e}")
        return False


async def _update_pattern_count(subsystem: str, features: dict, outcome: str) -> None:
    """
    Upsert the rolling pattern counter for this feature combination.
    
    This is the "local pattern learner" — it simply counts observations
    per feature hash. No ML, no model — just counting.
    """
    try:
        supabase = get_supabase()
        feature_hash = hash_features(features, subsystem)
        
        # Try to find existing pattern row
        existing = supabase.table("subsystem_patterns") \
            .select("id, total_count, correct_count, corrected_count") \
            .eq("subsystem", subsystem) \
            .eq("feature_hash", feature_hash) \
            .maybe_single() \
            .execute()
        
        now = datetime.now(timezone.utc).isoformat()
        
        if existing and existing.data:
            row = existing.data
            supabase.table("subsystem_patterns").update({
                "total_count": row["total_count"] + 1,
                "correct_count": row["correct_count"] + (1 if outcome in ("correct", "confirmed") else 0),
                "corrected_count": row["corrected_count"] + (1 if outcome in ("corrected", "rejected") else 0),
                "confidence": (row["correct_count"] + (1 if outcome in ("correct", "confirmed") else 0)) / (row["total_count"] + 1),
                "last_seen": now,
            }).eq("id", row["id"]).execute()
        else:
            supabase.table("subsystem_patterns").insert({
                "subsystem": subsystem,
                "feature_hash": feature_hash,
                "feature_json": features,
                "total_count": 1,
                "correct_count": 1 if outcome in ("correct", "confirmed") else 0,
                "corrected_count": 1 if outcome in ("corrected", "rejected") else 0,
                "confidence": 1.0 if outcome in ("correct", "confirmed") else 0.0,
                "last_seen": now,
            }).execute()
    except Exception as e:
        # Fail-open
        audit_log_sync("telemetry", "WARNING", f"_update_pattern_count failed: {e}")


async def get_pattern_summary(
    subsystem: str,
    min_observations: int = MIN_PATTERN_OBSERVATIONS,
    max_patterns: int = 10,
    days_back: int = 30,
) -> list[dict]:
    """
    Fetch top patterns for a subsystem, sorted by confidence descending.
    
    Returns list of dicts:
    [
        {
            "subsystem": "entity_extraction",
            "features": {"source": "telegram", "node_type": "person", "has_context": True},
            "total_count": 42,
            "confidence": 0.95,
            "recommendation": "auto_approve",
            "rule": "Person nodes from email with context: 42/42 approved (100%)"
        },
        ...
    ]
    """
    try:
        supabase = get_supabase()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        
        # Get patterns from the counters table
        rows = supabase.table("subsystem_patterns") \
            .select("*") \
            .eq("subsystem", subsystem) \
            .gte("total_count", min_observations) \
            .gte("last_seen", cutoff) \
            .order("confidence", desc=True) \
            .limit(max_patterns) \
            .execute()
        
        result = []
        for row in (rows.data or []):
            features = row.get("feature_json", {})
            total = row["total_count"]
            correct = row["correct_count"]
            confidence = correct / total if total > 0 else 0
            
            recommendation = "review"
            if confidence >= CONFIDENCE_AUTO_APPLY and total >= 10:
                recommendation = "auto_approve" if correct > total / 2 else "auto_reject"
            elif confidence >= CONFIDENCE_SUGGEST:
                recommendation = "suggest"
            
            # Build human-readable rule
            feature_parts = [f"{k}={v}" for k, v in features.items() if v]
            rule = f"{', '.join(feature_parts)}: {correct}/{total} ({confidence:.0%})"
            
            result.append({
                "subsystem": subsystem,
                "features": features,
                "total_count": total,
                "correct_count": correct,
                "confidence": confidence,
                "recommendation": recommendation,
                "rule": rule,
            })
        
        return result
    except Exception as e:
        audit_log_sync("telemetry", "WARNING", f"get_pattern_summary failed: {e}")
        return []


async def compute_pattern_confidence(
    features: dict,
    subsystem: str,
) -> dict:
    """
    Given a feature set, look up the stored pattern and return confidence.
    
    This is the runtime inference function used by subsystems to decide
    whether to auto-apply, suggest, or go to review.
    
    Args:
        features: Current item's features
        subsystem: Subsystem name
    
    Returns:
        {
            "confidence": 0.0-1.0,        # predicted accuracy
            "total_observations": int,     # how many times we've seen this
            "recommendation": str,         # "approve" | "reject" | "suggest" | "review"
            "rule": str                    # human-readable summary
        }
    """
    try:
        supabase = get_supabase()
        feature_hash = hash_features(features, subsystem)
        
        row = supabase.table("subsystem_patterns") \
            .select("total_count, correct_count, corrected_count, feature_json") \
            .eq("subsystem", subsystem) \
            .eq("feature_hash", feature_hash) \
            .maybe_single() \
            .execute()
        
        if not row.data or row.data["total_count"] < MIN_PATTERN_OBSERVATIONS:
            return {
                "confidence": 0.0,
                "total_observations": 0,
                "recommendation": "review",
                "rule": "Insufficient data",
            }
        
        total = row.data["total_count"]
        correct = row.data["correct_count"]
        confidence = correct / total if total > 0 else 0
        
        recommendation = "review"
        if confidence >= CONFIDENCE_AUTO_APPLY and total >= 10:
            recommendation = "approve" if correct > total / 2 else "reject"
        elif confidence >= CONFIDENCE_SUGGEST:
            recommendation = "suggest"
        
        return {
            "confidence": confidence,
            "total_observations": total,
            "recommendation": recommendation,
            "rule": f"{correct}/{total} ({confidence:.0%})",
        }
    except Exception as e:
        audit_log_sync("telemetry", "WARNING", f"compute_pattern_confidence failed: {e}")
        return {"confidence": 0.0, "total_observations": 0, "recommendation": "review", "rule": "Error"}


async def weekly_synthesis() -> dict:
    """
    Run across ALL subsystems. Produce a structured report for the weekend briefing.
    
    Returns:
        {
            "patterns": [...],  # top patterns per subsystem
            "drift": [...],     # significant changes from last week
            "recommendations": [...]  # suggested config changes
        }
    """
    SUBSYSTEMS = [
        "classification", "entity_extraction", "decision_pulse",
        "task_routing", "email_pipeline", "practices"
    ]
    
    all_patterns = []
    all_drift = []
    all_recommendations = []
    
    for subsystem in SUBSYSTEMS:
        patterns = await get_pattern_summary(subsystem, min_observations=3, max_patterns=5)
        
        if patterns:
            all_patterns.extend(patterns)
            
            # Check for drift: compare confidence to stored baseline
            try:
                supabase = get_supabase()
                baseline_key = f"pattern_baseline:{subsystem}"
                baseline_res = supabase.table("core_config") \
                    .select("content") \
                    .eq("key", baseline_key) \
                    .maybe_single() \
                    .execute()
                
                if baseline_res and baseline_res.data:
                    import json
                    baseline = json.loads(baseline_res.data["content"])
                    for p in patterns:
                        prev = baseline.get(p["features"].get("node_type", "") or 
                                           p["features"].get("source", "") or 
                                           list(p["features"].keys())[0])
                        if prev:
                            prev_conf = prev.get("confidence", 0)
                            if abs(p["confidence"] - prev_conf) > 0.20:
                                all_drift.append({
                                    "subsystem": subsystem,
                                    "signal": f"{p['rule']} (was {prev_conf:.0%} last week)",
                                    "delta": p["confidence"] - prev_conf,
                                })
                
                # Store this week's patterns as new baseline
                import json
                baseline_data = {}
                for p in patterns:
                    key = p["features"].get("node_type", "") or \
                          p["features"].get("source", "") or \
                          f"pattern_{p['features'].get('subsystem', subsystem)}"
                    baseline_data[key] = {
                        "confidence": p["confidence"],
                        "total_count": p["total_count"],
                    }
                supabase.table("core_config").upsert({
                    "key": baseline_key,
                    "content": json.dumps(baseline_data),
                }, on_conflict="key").execute()
            except Exception:
                pass
            
            # Build recommendations
            for p in patterns:
                if p["recommendation"] == "auto_approve":
                    all_recommendations.append(
                        f"Auto-approve: {p['rule']}"
                    )
                elif p["recommendation"] == "auto_reject":
                    all_recommendations.append(
                        f"Auto-reject: {p['rule']}"
                    )
    
    # Sort patterns by confidence descending
    all_patterns.sort(key=lambda p: p["confidence"], reverse=True)
    
    return {
        "patterns": all_patterns[:10],  # top 10 across all subsystems
        "drift": all_drift[:5],
        "recommendations": all_recommendations[:5],
    }
```

---

#### Step 1.3 — Classification Sensor

**File:** `core/webhook/dispatch.py`

**Change 1 — `resolve_disambiguation()` (lines 734-776):**

Add telemetry emission after the `FEEDBACK_OVERRIDE` audit_log_sync call (after line 755):

```python
# --- OLD (line 755) ---
audit_log_sync("webhook", "INFO",
    f"FEEDBACK_OVERRIDE: user corrected '{prev_intent}' → '{intent}' | text='{original[:80]}'")

# --- NEW (add after line 755) ---
audit_log_sync("webhook", "INFO",
    f"FEEDBACK_OVERRIDE: user corrected '{prev_intent}' → '{intent}' | text='{original[:80]}'")
# Emit telemetry for meta-cognitive learning
try:
    from core.lib.telemetry import emit_observation
    await emit_observation(
        subsystem="classification",
        event_type="correction",
        features={
            "source": "telegram",
            "entity_count": len(last_clarification.get("classification", {}).get("entity", "") or ""),
            "has_url": bool(re.search(r"https?://", original)),
            "word_count": len(original.split()),
            "has_time": bool(re.search(r"\b(\d{1,2}:\d{2}|tomorrow|today|next week)\b", original.lower())),
            "confidence": last_clarification.get("classification", {}).get("confidence", 0.5),
        },
        predicted=prev_intent,
        actual=intent,
        outcome="corrected",
        confidence=last_clarification.get("classification", {}).get("confidence"),
        session_id=session_id,
        source="webhook",
    )
except ImportError:
    pass  # Fail-open: telemetry module not yet available
except Exception:
    pass  # Fail-open: telemetry emission should never crash
```

**Change 2 — `resolve_task_note_confirmation()` (lines 778-804):**

Same pattern — add after line 796:

```python
# OLD (line 796)
audit_log_sync("webhook", "INFO",
    f"FEEDBACK_OVERRIDE: user corrected '{prev_intent}' → '{intent}' | text='{original[:80]}'")

# NEW (add after line 796)
audit_log_sync("webhook", "INFO",
    f"FEEDBACK_OVERRIDE: user corrected '{prev_intent}' → '{intent}' | text='{original[:80]}'")
try:
    from core.lib.telemetry import emit_observation
    await emit_observation(
        subsystem="classification",
        event_type="correction",
        features={
            "source": "telegram",
            "word_count": len(original.split()),
            "confidence": classification.get("confidence", 0.5),
        },
        predicted=prev_intent,
        actual=intent,
        outcome="corrected",
        confidence=classification.get("confidence"),
        session_id=session_id,
        source="webhook",
    )
except Exception:
    pass
```

**Change 3 — `classify_intent()` `SAFE_HOLD_CLASSIFICATION` path (classify.py lines 117-121):**

```python
# OLD (classify.py line 121)
return SAFE_HOLD_CLASSIFICATION

# NEW
from core.lib.telemetry import emit_observation
# Only emit if we have a session context (available from caller)
try:
    await emit_observation(
        subsystem="classification",
        event_type="failure",
        features={
            "text_length": len(text),
            "has_url": bool(re.search(r"https?://", text)),
        },
        predicted="SAFE_HOLD",
        actual=None,
        outcome="failed",
        source="classify",
    )
except Exception:
    pass
return SAFE_HOLD_CLASSIFICATION
```

---

#### Step 1.4 — Entity Extraction Sensor

**File:** `core/pulse/graph.py`

**Change 1 — `process_graph_pending_decision()` rejection path (lines 407-425):**

Add telemetry after `status='rejected'` update (after line 411):

```python
# OLD — after line 411
return {"success": True, "action": "rejected", "message": f"Rejected node and related edges for {label}"}

# NEW
try:
    from core.lib.telemetry import emit_observation
    await emit_observation(
        subsystem="entity_extraction",
        event_type="rejection",
        features={
            "source": pending_item.get("source_tag") or pending_item.get("metadata", {}).get("source", "unknown"),
            "node_type": pending_item.get("type", "unknown"),
            "has_context": bool(pending_item.get("source_text")),
        },
        predicted="pending",
        actual="rejected",
        outcome="rejected",
        session_id=None,
        source="decision_pulse",
    )
except Exception:
    pass
return {"success": True, "action": "rejected", "message": f"Rejected node and related edges for {label}"}
```

**Change 2 — Same function, approval path (around line 463, after `record_decision` call):**

```python
# OLD — after the record_decision block around line 458-463
                    except Exception as dec_err:
                        audit_log_sync("pulse", "WARNING", f"Failed to record graph node decision: {dec_err}")
                    # Cascade auto-approve related concepts...

# NEW — add after the audit_log_sync
                    except Exception as dec_err:
                        audit_log_sync("pulse", "WARNING", f"Failed to record graph node decision: {dec_err}")
                    # Emit telemetry
                    try:
                        from core.lib.telemetry import emit_observation
                        await emit_observation(
                            subsystem="entity_extraction",
                            event_type="approval",
                            features={
                                "source": pending_item.get("source_tag") or pending_item.get("metadata", {}).get("source", "unknown"),
                                "node_type": pending_item.get("type", "unknown"),
                                "has_context": bool(pending_item.get("source_text")),
                            },
                            predicted="pending",
                            actual="approved",
                            outcome="confirmed",
                            source="decision_pulse",
                        )
                    except Exception:
                        pass
                    # Cascade auto-approve related concepts...
```

**Change 3 — `process_pending_edge_decision()` rejection path (around line 519):**

```python
# OLD
return {"success": True, "action": "rejected", "message": "Rejected edge."}

# NEW
try:
    from core.lib.telemetry import emit_observation
    await emit_observation(
        subsystem="entity_extraction",
        event_type="rejection",
        features={
            "source": pe.get("source_table", "unknown"),
            "relationship_type": pe.get("relationship", "unknown"),
            "source_type": pe.get("source_type"),
            "target_type": pe.get("target_type"),
        },
        predicted="pending",
        actual="rejected",
        outcome="rejected",
        source="decision_pulse",
    )
except Exception:
    pass
return {"success": True, "action": "rejected", "message": "Rejected edge."}
```

**Change 4 — Same function, approval path (around line 570, after `record_decision` block):**

```python
# NEW — add after the audit_log_sync for edge approval record
                    except Exception as dec_err:
                        audit_log_sync("pulse", "WARNING", f"Failed to record graph edge decision: {dec_err}")
                    # Emit telemetry
                    try:
                        from core.lib.telemetry import emit_observation
                        await emit_observation(
                            subsystem="entity_extraction",
                            event_type="approval",
                            features={
                                "source": pe.get("source_table", "unknown"),
                                "relationship_type": rel,
                                "source_type": pe.get("source_type"),
                                "target_type": pe.get("target_type"),
                            },
                            predicted="pending",
                            actual="approved",
                            outcome="confirmed",
                            source="decision_pulse",
                        )
                    except Exception:
                        pass
```

---

#### Step 1.5 — Decision Pulse Sensor

**File:** `core/webhook/utils.py`

**Change 1 — `process_channel_pending_decision()` (lines 12-73):**

Add telemetry after `record_decision` call (after line 60):

```python
# OLD (line 60-63)
    except Exception as dec_err:
        audit_log_sync("webhook", "WARNING", f"Failed to record channel decision: {dec_err}")

    return {"success": True, ...}

# NEW
    except Exception as dec_err:
        audit_log_sync("webhook", "WARNING", f"Failed to record channel decision: {dec_err}")
    
    # Emit telemetry for meta-cognitive learning
    try:
        from core.lib.telemetry import emit_observation
        await emit_observation(
            subsystem="decision_pulse",
            event_type="approval" if is_approved else "rejection",
            features={
                "channel": channel,
                "has_project": bool(msg.get("suggested_project")),
                "has_sender_name": bool(sender_name),
            },
            predicted=None,
            actual="approved" if is_approved else "rejected",
            outcome="confirmed",
            source="decision_pulse",
        )
    except Exception:
        pass

    return {"success": True, ...}
```

**File:** `core/pulse/engine.py`

**Change 2 — `process_decision_pulse()` (lines 248-396):**

No telemetry needed here — the individual decision handlers (`process_graph_pending_decision`, `process_channel_pending_decision`) already emit per-item telemetry. The decision pulse is a batch UI, not a learning site.

---

#### Step 1.6 — Task Routing Sensor

**File:** `core/agents/quick_process.py` or wherever task project/org reassignments happen

The key site is when Danny changes a task's `project_id` or `organization_id`. The `/ed` command handler in `handler.py` (line 609+ — `handle_ed_command`) and `core/webhook/commands.py` are the entry points.

**File:** `core/webhook/commands.py`

After a task update is applied (project_id or organization_id changed), add:

```python
try:
    from core.lib.telemetry import emit_observation
    await emit_observation(
        subsystem="task_routing",
        event_type="correction",
        features={
            "keywords": [w for w in title.lower().split() if len(w) > 3][:5],
            "source": source,
            "has_people": "any person name" in title.lower(),  # simplified
        },
        predicted={"project": old_project_name, "org": old_org_name},
        actual={"project": new_project_name, "org": new_org_name},
        outcome="corrected",
        source="webhook",
    )
except Exception:
    pass
```

---

#### Step 1.7 — Test Strategy for Phase 1

**New test file:** `tests/unit/test_telemetry.py`

```python
"""
Tests for Tier 5 Meta-Cognitive Learning Layer telemetry module.

Tests:
T1 — emit_observation writes to subsystem_telemetry
T2 — pattern counter increments correctly
T3 — hash_features is deterministic
T4 — get_pattern_summary returns sorted by confidence
T5 — compute_pattern_confidence returns correct values
T6 — weekly_synthesis produces structured output
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from core.lib.telemetry import (
    emit_observation, hash_features, get_pattern_summary,
    compute_pattern_confidence, weekly_synthesis
)


@pytest.mark.asyncio
async def test_hash_features_is_deterministic():
    """T3: Same features + subsystem always produce same hash."""
    features = {"source": "telegram", "node_type": "person", "has_context": True}
    h1 = hash_features(features, "entity_extraction")
    h2 = hash_features(features, "entity_extraction")
    assert h1 == h2
    assert len(h1) == 16


@pytest.mark.asyncio
async def test_hash_features_different_subsystems():
    """T3b: Different subsystems with same features produce different hashes."""
    f = {"source": "email"}
    h1 = hash_features(f, "entity_extraction")
    h2 = hash_features(f, "classification")
    assert h1 != h2


@pytest.mark.asyncio
async def test_emit_observation_stores_row():
    """T1: emit_observation inserts into the DB."""
    # Mock supabase
    with patch("core.lib.telemetry.get_supabase") as mock_get_db:
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_insert = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.insert.return_value = mock_insert
        mock_get_db.return_value = mock_supabase
        
        result = await emit_observation(
            subsystem="classification",
            event_type="correction",
            features={"source": "telegram", "word_count": 5},
            predicted="NOTE",
            actual="TASK",
            outcome="corrected",
            confidence=0.6,
            source="test",
        )
        
        assert result is True
        mock_supabase.table.assert_called_with("subsystem_telemetry")
        mock_table.insert.assert_called_once()


@pytest.mark.asyncio
async def test_compute_pattern_confidence_insufficient():
    """T5: With <3 observations, returns 'review'."""
    with patch("core.lib.telemetry.get_supabase") as mock_get_db:
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        # Simulate empty result
        mock_select = MagicMock()
        mock_select.maybe_single.return_value = MagicMock(data=None)
        mock_table.select.return_value = mock_select
        mock_get_db.return_value = mock_supabase
        
        result = await compute_pattern_confidence(
            {"source": "email"}, "entity_extraction"
        )
        assert result["recommendation"] == "review"
        assert result["confidence"] == 0.0


@pytest.mark.asyncio
async def test_emit_observation_fail_open():
    """T1b: emit_observation failure doesn't crash."""
    with patch("core.lib.telemetry.get_supabase") as mock_get_db:
        mock_get_db.side_effect = Exception("DB down")
        result = await emit_observation(
            subsystem="classification",
            event_type="correction",
            features={"source": "test"},
            outcome="corrected",
        )
        assert result is False  # fail-open returns False, doesn't raise
```

---

### Phase 2: Pattern Extraction + Weekly Synthesis

**Goal:** Run pattern extraction from telemetry data, produce interpretable rules, surface in weekend briefings.

**Duration:** ~1-2 sessions

---

#### Step 2.1 — Module: `core/lib/pattern_extractor.py` (NEW)

```python
"""
Pattern extraction and drift detection for Tier 5 Meta-Cognitive Layer.

Provides:
- extract_patterns() — per-subsystem pattern extraction
- detect_drift() — compare this week to last week
- build_transparency_report() — "What I Learned" section for briefings
"""

from datetime import datetime, timezone, timedelta
from core.lib.telemetry import get_pattern_summary, weekly_synthesis
from core.lib.audit_logger import audit_log_sync


async def extract_patterns(
    subsystem: str,
    min_observations: int = 3,
    max_patterns: int = 5,
) -> list[dict]:
    """
    Extract interpretable patterns from telemetry data.
    
    Wraps get_pattern_summary with additional metadata for the transparency report.
    
    Returns patterns sorted by confidence descending.
    """
    patterns = await get_pattern_summary(
        subsystem=subsystem,
        min_observations=min_observations,
        max_patterns=max_patterns,
    )
    return patterns


async def detect_drift(subsystem: str) -> list[dict]:
    """
    Compare this week's patterns to last week's stored baseline.
    
    Returns list of drift signals where confidence changed by >20%.
    Each signal includes the direction and magnitude of change.
    """
    try:
        from core.services.db import get_supabase
        import json
        
        supabase = get_supabase()
        
        # Get this week's patterns
        current = await extract_patterns(subsystem, min_observations=3)
        
        # Get last week's baseline from core_config
        baseline_key = f"pattern_baseline:{subsystem}"
        baseline_res = supabase.table("core_config") \
            .select("content") \
            .eq("key", baseline_key) \
            .maybe_single() \
            .execute()
        
        if not baseline_res or not baseline_res.data:
            return []
        
        baseline = json.loads(baseline_res.data["content"])
        
        drift_signals = []
        for pattern in current:
            # Find the matching baseline entry
            feature_key = None
            for fk in baseline:
                if fk in str(pattern.get("features", {})):
                    feature_key = fk
                    break
            
            if feature_key:
                prev = baseline[feature_key]
                prev_conf = prev.get("confidence", 0)
                curr_conf = pattern["confidence"]
                delta = curr_conf - prev_conf
                
                if abs(delta) > 0.20:
                    drift_signals.append({
                        "subsystem": subsystem,
                        "pattern": pattern["rule"],
                        "was": prev_conf,
                        "now": curr_conf,
                        "delta": delta,
                        "direction": "up" if delta > 0 else "down",
                    })
        
        return drift_signals
    except Exception as e:
        audit_log_sync("pattern_extractor", "WARNING", f"detect_drift failed: {e}")
        return []


async def build_transparency_report() -> str:
    """
    Build the "What I Learned This Week" section for weekend briefings.
    
    Returns a formatted string suitable for Telegram.
    """
    synthesis = await weekly_synthesis()
    
    if not synthesis["patterns"]:
        return ""
    
    lines = ["🧠 *What I Learned This Week*"]
    lines.append("")
    
    # Group patterns by subsystem
    by_subsystem = {}
    for p in synthesis["patterns"]:
        sub = p["subsystem"]
        if sub not in by_subsystem:
            by_subsystem[sub] = []
        by_subsystem[sub].append(p)
    
    for subsystem, patterns in by_subsystem.items():
        emoji_map = {
            "classification": "🏷️",
            "entity_extraction": "🕸️",
            "decision_pulse": "📋",
            "task_routing": "🔀",
            "email_pipeline": "📨",
            "practices": "🏃",
        }
        emoji = emoji_map.get(subsystem, "📊")
        label = subsystem.replace("_", " ").title()
        lines.append(f"{emoji} *{label}:*")
        
        for p in patterns[:3]:  # max 3 per subsystem
            icon = "✅" if p["recommendation"] == "auto_approve" else \
                   "❌" if p["recommendation"] == "auto_reject" else "💡"
            lines.append(f"  {icon} {p['rule']}")
        
        lines.append("")
    
    # Add drift signals
    if synthesis["drift"]:
        lines.append("⚠️ *Pattern Changes (Drift Detected):*")
        for d in synthesis["drift"][:3]:
            arrow = "↑" if d["delta"] > 0 else "↓"
            lines.append(f"  {arrow} {d['pattern']} ({d['was']:.0%} → {d['now']:.0%})")
        lines.append("")
    
    # Add recommendations
    if synthesis["recommendations"]:
        lines.append("🤖 *Recommendations:*")
        for r in synthesis["recommendations"][:3]:
            lines.append(f"  • {r}")
            lines.append(f"    → Auto-apply? [✅](approve) [❌](reject)")
        lines.append("")
    
    return "\n".join(lines)
```

---

#### Step 2.2 — Sentinel Piggyback

**File:** `core/pulse/sentinel.py`

**Change:** Add pattern extraction as a Sunday piggyback step (around line 290-300, before the existing pattern detection block):

```python
# --- PIGGYBACK: Tier 5 Meta-Cognitive Pattern Extraction (Sunday only) ---
try:
    if now.weekday() == 6:  # Sunday
        last_pattern_pulse = supabase.table("audit_logs") \
            .select("id") \
            .eq("service", "sentinel") \
            .ilike("message", "%meta_cognitive_synthesis%") \
            .gte("created_at", (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()) \
            .limit(1) \
            .execute()
        if not last_pattern_pulse.data:
            from core.lib.pattern_extractor import build_transparency_report
            report = await build_transparency_report()
            if report:
                # Store in core_config for the weekend briefing to consume
                supabase.table("core_config").upsert({
                    "key": "learning_transparency_report",
                    "content": report,
                }, on_conflict="key").execute()
                audit_log_sync("sentinel", "INFO", "meta_cognitive_synthesis: Transparency report stored")
            else:
                audit_log_sync("sentinel", "INFO", "meta_cognitive_synthesis: No significant patterns yet")
except Exception as e:
    audit_log_sync("sentinel", "WARNING", f"Meta-cognitive synthesis error: {e}")
```

---

#### Step 2.3 — Weekend Briefing Integration

**File:** `core/pulse/engine.py`

**Change:** In the weekend briefing generation (around line 1420-1440, where `rhythms_text` is appended), also append the transparency report:

```python
# --- 🧠 META-COGNITIVE TRANSPARENCY REPORT (Weekends only) ---
if is_weekend:
    try:
        report_res = supabase.table("core_config") \
            .select("content") \
            .eq("key", "learning_transparency_report") \
            .maybe_single() \
            .execute()
        if report_res and report_res.data:
            report_text = report_res.data["content"]
            if report_text and briefing_text:
                # Append after rhythms section
                briefing_text += "\n\n" + report_text
            audit_log_sync("pulse", "INFO", "Appended learning transparency report to briefing")
    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"Learning report append failed: {e}")
```

---

### Phase 3: Auto-Apply + Drift Detection

**Goal:** Start auto-applying high-confidence patterns with HITL gates. Detect when patterns drift.

**Note:** Phase 3 should only start after Phase 1 has been running for at least 2 weeks (accumulating telemetry data), and Danny has confirmed patterns are accurate via the weekend transparency reports.

**Duration:** ~1 session after 2+ weeks of data

---

#### Step 3.1 — Configuration Injection Points

**Pattern Type:** "Person nodes from email with context = 95% approve"

**Injection Point:** `core/pulse/graph.py` — `process_graph_pending_decision()` approval path

Add before the `create_graph_node_with_db_record()` call:

```python
# Before auto-approving, check if we have a learned pattern
try:
    from core.lib.telemetry import compute_pattern_confidence
    confidence = await compute_pattern_confidence(
        features={
            "source": pending_item.get("source_tag", "unknown"),
            "node_type": pending_item.get("type", "unknown"),
            "has_context": bool(pending_item.get("source_text")),
        },
        subsystem="entity_extraction",
    )
    if confidence["recommendation"] == "approve":
        # Skip pending entirely — go straight to creation
        audit_log_sync("pulse", "INFO",
            f"Auto-approving {pending_item['label']} ({confidence['rule']})")
        # ... auto-approve without sending to Decision Pulse ...
except Exception:
    pass
```

**Pattern Type:** "Concept nodes from backfill = 100% reject"

**Injection Point:** `core/pulse/entity_extractor.py` — before creating concept nodes

```python
# Before creating a concept node during extraction
try:
    from core.lib.telemetry import compute_pattern_confidence
    confidence = await compute_pattern_confidence(
        features={"source": "backfill", "node_type": "concept", "has_context": False},
        subsystem="entity_extraction",
    )
    if confidence["recommendation"] == "reject":
        audit_log_sync("pulse", "INFO",
            f"Skipping concept node creation (pattern: {confidence['rule']})")
        return [], []  # Skip entirely
except Exception:
    pass
```

---

#### Step 3.2 — HITL Gate for Auto-Apply

Before any pattern is auto-applied, Danny gets a one-time confirmation via the weekend briefing:

```
🧠 *What I Learned This Week*

🕸️ Entity Extraction:
  ✅ Person nodes from email with context: 42/42 approved (100%)
  → Auto-approve future matches? [✅ Yes] [❌ No] [🔍 Show examples]

📋 Decision Pulse:
  ✅ WhatsApp items with project name: 28/30 approved (93%)
  → Auto-approve future matches? [✅ Yes] [❌ No] [🔍 Show examples]
```

This is rendered by the `build_transparency_report()` function with inline keyboard callbacks (`approve_pattern_entity_extraction`, `reject_pattern_entity_extraction`, etc.).

---

#### Step 3.3 — Drift Detection in Sentinel

Already built into `weekly_synthesis()` in `telemetry.py` — compares this week's pattern confidence to last week's stored baseline. Flags any changes >20%.

---

## 5. File Changes — Complete Summary

### New Files

| File | Lines | Purpose |
|---|---|---|
| `db/21_subsystem_telemetry.sql` | ~25 | Schema: `subsystem_telemetry` + `subsystem_patterns` tables |
| `core/lib/telemetry.py` | ~230 | `emit_observation()`, `hash_features()`, `get_pattern_summary()`, `compute_pattern_confidence()`, `weekly_synthesis()` |
| `core/lib/pattern_extractor.py` | ~140 | `extract_patterns()`, `detect_drift()`, `build_transparency_report()` |
| `tests/unit/test_telemetry.py` | ~100 | 6+ unit tests for telemetry module |

### Modified Files (Phase 1 only)

| File | Lines Changed | Change |
|---|---|---|
| `core/webhook/dispatch.py` | +12 (after L755, after L796) | Add `emit_observation()` in `resolve_disambiguation()` and `resolve_task_note_confirmation()` |
| `core/webhook/classify.py` | +10 (around L121) | Add `emit_observation()` on `SAFE_HOLD_CLASSIFICATION` return |
| `core/pulse/graph.py` | +40 (around L411, L463, L519, L570) | Add `emit_observation()` in `process_graph_pending_decision()` approve/reject and `process_pending_edge_decision()` approve/reject |
| `core/webhook/utils.py` | +12 (around L63) | Add `emit_observation()` in `process_channel_pending_decision()` |
| `core/webhook/commands.py` | +14 (after task update) | Add `emit_observation()` for task routing corrections |

### Modified Files (Phase 2)

| File | Lines Changed | Change |
|---|---|---|
| `core/pulse/sentinel.py` | +20 (around L290) | Add meta-cognitive synthesis as Sunday piggyback |
| `core/pulse/engine.py` | +12 (around L1440) | Append transparency report to weekend briefing |

### Files NOT Changed

- `core/llm/` — no changes to LLM chain, models, or fallback logic
- `api/index.py` — no new API endpoints
- `frontend/` — no UI changes in Phase 1-2
- `.github/workflows/` — no new workflows
- `core/retrieval/` — no changes to retrieval pipeline
- `core/services/` — no changes to external integrations

---

## 6. Concrete Examples

### Example 1: Classification Correction Flow

**User:** Sends "Follow up with Equisoft on pricing"

**System:** Classifies as `NOTE` (0.6 confidence)

**User:** Corrects via inline keyboard → `TASK`

**Telemetry emitted:**
```json
{
    "subsystem": "classification",
    "event_type": "correction",
    "features": {
        "source": "telegram",
        "entity_count": 1,
        "has_url": false,
        "word_count": 6,
        "has_time": false,
        "confidence": 0.6
    },
    "predicted": "NOTE",
    "actual": "TASK",
    "outcome": "corrected",
    "confidence": 0.6
}
```

**Pattern after 10+ similar corrections:** "Telegram messages with entity count ≥1 AND confidence <0.7: 8/10 corrected from NOTE→TASK" → inject as learned rule, suggest lowering the TASK confidence threshold for entity-bearing messages.

### Example 2: Entity Extraction Approval Flow

**User:** Approves `g42` (person node "Alex Johnson" from email with context "client at Equisoft")

**Telemetry emitted:**
```json
{
    "subsystem": "entity_extraction",
    "event_type": "approval",
    "features": {
        "source": "email_ingest",
        "node_type": "person",
        "has_context": true
    },
    "predicted": "pending",
    "actual": "approved",
    "outcome": "confirmed"
}
```

**Pattern after 42 identical approvals:** "Person nodes from email_ingest with context: 42/42 approved (100%)" → auto-approve without human review.

### Example 3: Entity Extraction Rejection Flow

**User:** Rejects `g88` (concept node "Agile Methodology" from backfill)

**Telemetry emitted:**
```json
{
    "subsystem": "entity_extraction",
    "event_type": "rejection",
    "features": {
        "source": "backfill",
        "node_type": "concept",
        "has_context": false
    },
    "predicted": "pending",
    "actual": "rejected",
    "outcome": "rejected"
}
```

**Pattern after 15 identical rejections:** "Concept nodes from backfill without context: 15/15 rejected (100%)" → stop creating concept nodes from backfill entirely.

### Example 4: Weekly Transparency Report

```
🧠 *What I Learned This Week*

🏷️ Classification:
  ✅ Messages with ≥1 entity + confidence <0.7: 12/15 corrected NOTE→TASK (80%)
  💡 Short messages (<5 words): 8/10 correct as NOISE (80%)

🕸️ Entity Extraction:
  ✅ Person nodes from email with context: 18/18 approved (100%)
  ❌ Concept nodes from backfill: 12/12 rejected (100%)

📋 Decision Pulse:
  ✅ WhatsApp items with project name: 15/16 approved (94%)
  💡 Call extracts without project: 6/10 rejected (60%)

🤖 *Recommendations:*
  • Auto-approve person nodes from email with context? [✅] [❌]
  • Stop creating concept nodes from backfill? [✅] [❌]
```

---

## 7. Success Metrics

| Metric | Current | Target (Month 1) | Target (Month 3) |
|---|---|---|---|
| Subsystems with feedback | 1 (classification only) | 4 | 8 |
| Telemetry observations per week | 0 | ~50 | ~200+ |
| Patterns surfaced in transparency report | 0 | 3-5 | 8-12 |
| Auto-applied decisions (Phase 3) | 0 | 0 (shadow only) | ~20% of routine items |
| Danny's time in Decision Pulse | 30-45 min/day | 30-45 min (shadow) | 15-20 min |
| Cross-dimensional patterns found | 0 | 0 | 2-3/week |
| Auto-apply reversal rate | N/A | N/A | <1% |

---

## 8. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| emit_observation crashes the calling subsystem | Low | High | Fail-open: every call is wrapped in try/except with audit_log fallback |
| subsystem_telemetry grows too fast | Low | Low | Weekly cleanup of observations >90 days; index on (created_at) for efficient DELETE |
| Patterns suggest wrong auto-apply | Medium | Medium | Phase 3 requires HITL confirmation for each pattern before auto-apply |
| Pattern drift goes undetected | Low | Medium | Built-in drift detection compares weekly baselines; flags >20% changes |
| Danny ignores transparency report section | Medium | Low | Keep to 2-3 lines per subsystem; always include; make actionable with inline buttons |
| Low observation volume in first 2 weeks | High | Low | Patterns with <3 observations return "review" — no false confidence |
| Cross-dimensional spurious correlations | Medium | Low | Surface all correlations with standard warnings; Danny decides whether to act |

---

## 9. Implementation Order

### Session 1: Foundation
1. Write `db/21_subsystem_telemetry.sql` migration
2. Write `core/lib/telemetry.py` — all functions
3. Write `tests/unit/test_telemetry.py` — all 6 tests
4. Run migration on Supabase
5. Run tests, fix any issues

### Session 2: Classification Sensor
6. Add `emit_observation()` to `resolve_disambiguation()` in `dispatch.py`
7. Add `emit_observation()` to `resolve_task_note_confirmation()` in `dispatch.py`
8. Add `emit_observation()` to `classify.py` safe-hold path
9. Test by running a few classification corrections

### Session 3: Entity Extraction + Decision Pulse Sensors
10. Add `emit_observation()` to `process_graph_pending_decision()` approve/reject in `graph.py`
11. Add `emit_observation()` to `process_pending_edge_decision()` approve/reject in `graph.py`
12. Add `emit_observation()` to `process_channel_pending_decision()` in `utils.py`
13. Test by approving/rejecting a few pending items

### Session 4: Pattern Extraction Phase
14. Write `core/lib/pattern_extractor.py`
15. Add sentinel piggyback for weekly synthesis
16. Add weekend briefing integration
17. Review first transparency report with Danny

### Session 5: Auto-Apply Phase (2+ weeks after Phase 1)
18. Implement config injection points in `graph.py` and `entity_extractor.py`
19. Build HITL gate callbacks
20. Test auto-apply with Danny's approval

---

## 10. Rejected Alternatives

| Approach | Why Rejected |
|---|---|
| Vowpal Wabbit contextual bandit | C++ dependency; serverless cold starts; serialization complexity; black-box predictions |
| River online ML | No active learning module; less mature for binary preference learning |
| scikit-learn SGDClassifier | Must build exploration, active learning, cold start from scratch — same complexity as counting |
| Rationale capture as primary signal | Noisy and incomplete; Danny wouldn't write reasons every time |
| Single centralized preference model | Misses subsystem-specific patterns; can't tune per-subsystem knobs |
| Weekly batch-only pattern mining | Too slow — Danny wanted active querying for efficiency |
| LLM-based rule extraction as primary | Expensive (token cost); slow (1-2s per rule); less reliable than counting |
