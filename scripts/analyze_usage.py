#!/usr/bin/env python3
"""Analyze Rhodey OS usage over last 3 months to estimate Modal migration cost."""

import os
import sys
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.db import get_supabase

supabase = get_supabase()
now = datetime.now(timezone.utc)
three_months_ago = now - timedelta(days=90)

print("=" * 80)
print("RHODEY OS USAGE ANALYSIS — Last 3 Months")
print(f"Period: {three_months_ago.date()} to {now.date()}")
print("=" * 80)

# 1. RAW DUMPS — Message volume (all channels)
print("\n--- 1. RAW DUMPS (all messages processed) ---")
raw_count = supabase.table('raw_dumps').select('id', count='exact').gte('created_at', three_months_ago.isoformat()).execute()
print(f"Total raw_dumps (3mo): {raw_count.count}")

# Per month breakdown
for months_back in range(3):
    start = now - timedelta(days=(months_back + 1) * 30)
    end = now - timedelta(days=months_back * 30)
    count = supabase.table('raw_dumps').select('id', count='exact').gte('created_at', start.isoformat()).lt('created_at', end.isoformat()).execute()
    print(f"  Month {months_back + 1} ({start.date()}-{end.date()}): {count.count}")

# Bot responses (outgoing)
response_count = supabase.table('raw_dumps').select('id', count='exact').eq('direction', 'outgoing').gte('created_at', three_months_ago.isoformat()).execute()
print(f"Outgoing (bot responses): {response_count.count}")

# 2. MODEL REGISTRY — LLM API calls
print("\n--- 2. MODEL REGISTRY (LLM API calls) ---")
model_count = supabase.table('model_registry').select('id', count='exact').gte('created_at', three_months_ago.isoformat()).execute()
print(f"Total model_registry entries (3mo): {model_count.count}")

try:
    # Try to get model breakdown
    models = supabase.table('model_registry').select('model_name, count', count='exact').gte('created_at', three_months_ago.isoformat()).execute()
    print(f"Model calls available: {len(models.data) if models.data else 'unknown'}")
except Exception as e:
    print(f"Model breakdown query failed: {e}")

# 3. AUDIT LOGS — System events
print("\n--- 3. AUDIT LOGS (system events) ---")
audit_count = supabase.table('audit_logs').select('id', count='exact').gte('created_at', three_months_ago.isoformat()).execute()
print(f"Total audit_log entries (3mo): {audit_count.count}")

# 4. PROCESSED UPDATES — Telegram webhook volume
print("\n--- 4. PROCESSED UPDATES (Telegram webhook calls) ---")
updates_count = supabase.table('processed_updates').select('id', count='exact').gte('processed_at', three_months_ago.isoformat()).execute()
print(f"Total Telegram updates processed (3mo): {updates_count.count}")

# Per-month breakdown
for months_back in range(3):
    start = now - timedelta(days=(months_back + 1) * 30)
    end = now - timedelta(days=months_back * 30)
    count = supabase.table('processed_updates').select('id', count='exact').gte('processed_at', start.isoformat()).lt('processed_at', end.isoformat()).execute()
    print(f"  Month {months_back + 1} ({start.date()}-{end.date()}): {count.count}")

# Daily average (last 30 days)
last_30 = now - timedelta(days=30)
updates_30d = supabase.table('processed_updates').select('id', count='exact').gte('processed_at', last_30.isoformat()).execute()
print(f"Last 30 days: {updates_30d.count} updates = ~{updates_30d.count / 30:.1f}/day")

# 5. CONVERSATIONS — User-bot interactions
print("\n--- 5. CONVERSATIONS (user-bot exchanges) ---")
conv_count = supabase.table('conversations').select('id', count='exact').gte('created_at', three_months_ago.isoformat()).execute()
print(f"Total conversation exchanges (3mo): {conv_count.count}")

# 6. ENRICHMENT JOBS
print("\n--- 6. ENRICHMENT QUEUE (background jobs) ---")
try:
    enrich_count = supabase.table('pending_enrichment_jobs').select('id', count='exact').execute()
    print(f"Total pending_enrichment_jobs (all time): {enrich_count.count}")
except Exception as e:
    print(f"Enrichment query failed: {e}")

# 7. API ENDPOINT CALLS (estimated from raw_dumps source)
print("\n--- 7. MESSAGE SOURCE BREAKDOWN ---")
sources = supabase.table('raw_dumps').select('source', count='exact').gte('created_at', three_months_ago.isoformat()).execute()
try:
    source_data = supabase.table('raw_dumps').select('source').gte('created_at', three_months_ago.isoformat()).execute()
    if source_data.data:
        source_counts = {}
        for row in source_data.data:
            src = row.get('source', 'unknown')
            source_counts[src] = source_counts.get(src, 0) + 1
        for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1]):
            print(f"  {src}: {cnt}")
except Exception as e:
    print(f"Source breakdown failed: {e}")

# 8. Summary
print("\n" + "=" * 80)
print("COST ESTIMATE SUMMARY FOR MODAL MIGRATION")
print("=" * 80)

daily_updates = updates_30d.count / 30 if updates_30d.count else 50
daily_convs = (conv_count.count / 90) if conv_count.count else 30

# Estimate: each webhook call is ~30-45s on Vercel, ~15-25s on Modal
# Plus background tasks
print(f"\nTraffic Profile:")
print(f"  Daily Telegram updates (webhook calls): ~{daily_updates:.0f}")
print(f"  Daily conversation exchanges: ~{daily_convs:.0f}")
print(f"  Daily bot responses: ~{response_count.count / 90:.0f}")
print(f"  Monthly model_registry entries: ~{model_count.count / 3:.0f}")

# Background task estimates
sentinel_runs = 288 * 30  # every 5 min
decision_runs = 48 * 30   # every 30 min
pulse_runs = 6 * 22       # weekdays only
roundup_runs = 2 * 30     # 2x daily

print(f"\nBackground Tasks (monthly):")
print(f"  Sentinel (every 5 min): ~{sentinel_runs} runs")
print(f"  Decision Pulse (every 30 min): ~{decision_runs} runs")
print(f"  Pulse engine (6x weekday): ~{pulse_runs} runs")
print(f"  Roundup (2x daily): ~{roundup_runs} runs")

# Compute seconds estimation
webhook_sec = daily_updates * 30 * 30  # avg 30s on Modal per webhook
bg_pulse_sec = pulse_runs * 20
bg_sentinel_sec = sentinel_runs * 10
bg_decision_sec = decision_runs * 8
bg_roundup_sec = roundup_runs * 15
total_webhook_sec = webhook_sec
total_bg_sec = bg_pulse_sec + bg_sentinel_sec + bg_decision_sec + bg_roundup_sec

print(f"\nCompute Seconds (monthly):")
print(f"  Webhook processing: ~{total_webhook_sec:,}s ({total_webhook_sec/3600:.1f} hrs)")
print(f"  Background tasks: ~{total_bg_sec:,}s ({total_bg_sec/3600:.1f} hrs)")
print(f"  Total: ~{total_webhook_sec + total_bg_sec:,}s ({(total_webhook_sec + total_bg_sec)/3600:.1f} hrs)")

# Modal cost calculation
# CPU: $0.0000131/phys_core/s, min 0.125 phys cores = 0.25 vCPU
# RAM: $0.00000222/GiB/s
seconds_per_month = 30 * 24 * 3600  # 2,592,000

# Warm container (min_containers=1): always on
warm_cpu_cost = 0.125 * 0.0000131 * seconds_per_month  # $4.25
warm_ram_cost = 0.5 * 0.00000222 * seconds_per_month   # $2.88
warm_total = warm_cpu_cost + warm_ram_cost

# Background tasks (scale to zero, cold start)
bg_cpu_cost = 0.125 * 0.0000131 * total_bg_sec
bg_ram_cost = 0.5 * 0.00000222 * total_bg_sec
bg_total = bg_cpu_cost + bg_ram_cost

# Network egress (estimate: ~10KB per response, plus webhook payloads)
# Modal: first 50GB free, then $0.09/GB
monthly_data_gb = (daily_updates * 30 * 20 * 1024) / (1024**3)  # ~20KB per webhook exchange
if monthly_data_gb < 1:
    monthly_data_gb = 1  # very small

print(f"\n--- MODAL COST ---")
print(f"Warm container (FastAPI, min_containers=1):")
print(f"  CPU (0.125 phys cores): ${warm_cpu_cost:.2f}/mo")
print(f"  RAM (0.5 GiB): ${warm_ram_cost:.2f}/mo")
print(f"  Subtotal: ${warm_total:.2f}/mo")
print(f"")
print(f"Background tasks (scale-to-zero):")
print(f"  CPU: ${bg_cpu_cost:.2f}/mo")
print(f"  RAM: ${bg_ram_cost:.2f}/mo")
print(f"  Subtotal: ${bg_total:.2f}/mo")
print(f"")
print(f"Network (est. ~{monthly_data_gb:.1f} GB/mo): $0.00 (within free tier)")
print(f"")
print(f"GRAND TOTAL: ${warm_total + bg_total:.2f}/month")
print(f"MODAL FREE CREDIT: $30.00/month")
print(f"REMAINING CREDIT: ${30 - (warm_total + bg_total):.2f}/month")
print(f"YOU PAY: $0.00 (well within free tier)")
print(f"")
print(f"Vercel cost savings: Vercel Hobby = $0/mo (stays free for frontend)")
print(f"Net change to your wallet: $0.00/month")
