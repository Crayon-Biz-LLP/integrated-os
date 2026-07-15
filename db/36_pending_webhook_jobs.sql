-- Migration: pending_webhook_jobs table for async webhook processing
-- Replaces the unsafe asyncio.wait_for(55) + fire-and-forget pattern in api/index.py
-- which could timeout on Vercel's 60s hard limit.
--
-- Flow:
-- 1. Webhook endpoint receives Telegram update → INSERT into this table → return 200 immediately
-- 2. Dedicated consumer (/api/process-webhook-jobs) processes pending jobs every 15s
-- 3. Sentinel piggyback acts as catch-all for jobs the dedicated consumer missed
-- 4. Failed jobs retry up to 3 times, then escalate to dead_letter
--
-- Each job stores the full Telegram update_data as JSONB. The consumer
-- deserializes it and calls process_webhook(update_data) which internally
-- calls send_telegram to respond to the user.

CREATE TABLE IF NOT EXISTS pending_webhook_jobs (
    id BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    update_data JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'dead_letter')),
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

-- Index for the sweep query: status + created_at (FIFO)
CREATE INDEX IF NOT EXISTS idx_pending_webhook_jobs_status
    ON pending_webhook_jobs (status, created_at ASC);
