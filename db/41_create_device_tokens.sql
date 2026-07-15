-- Migration 41: Create device_tokens table for FCM push notifications
-- Flutter client already registers tokens via /api/register-device
-- Backend already reads from this table in send_push_notification()

CREATE TABLE IF NOT EXISTS device_tokens (
    token TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT 'android',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Allow service_role full access
GRANT ALL ON device_tokens TO service_role;
