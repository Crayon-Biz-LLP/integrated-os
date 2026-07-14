# Push Notification Service (FCM)

Adds Firebase Cloud Messaging push notifications to the Rhodey Flutter app. Every outgoing Telegram message triggers a fire-and-forget FCM push so the app receives responses instantly.

## Architecture

1. `send_telegram()` in `core/webhook/telegram.py` persists the response to `raw_dumps` and calls `push_notification.send_push()`.
2. `core/services/push_notification.py` constructs an FCM message with the response text and sends it via Firebase Admin SDK.
3. The Flutter app receives the push in `notification_service.dart`, which triggers an immediate briefing fetch via `onPushReceived`.
4. The `/api/briefing` endpoint exposes a `latest_response` field (latest outgoing raw_dump from the past hour) so the app can display persistent response cards.

## Key Files
- `core/services/push_notification.py` (NEW) — FCM send helper
- `core/webhook/telegram.py` — Push wire in send_telegram
- `api/briefing.py` — latest_response field
- `rhodey_app/lib/services/notification_service.dart` — onPushReceived callback
