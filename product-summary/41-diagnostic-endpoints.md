# Diagnostic Endpoints

Two health/debug endpoints added to `api/index.py`:

## `/api/briefing-ping`
Health check. Returns `{"status": "ok", "timestamp": "..."}`. Quick connectivity test.

## `/api/briefing-debug`
Full debug dump returns:
- Latest memory and task counts
- Current time + timezone context (IST)
- Active entity anchor (from conversation_threads)
- Recent outgoing raw_dumps
- Project/task breakdown

## `/api/register-device`
FCM device registration endpoint — stores FCM tokens for push notification targeting.
