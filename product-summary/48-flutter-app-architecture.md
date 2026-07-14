# Flutter App Architecture (Rhodey)

**Date**: Jul 7-13, 2026 | **Phase**: 25-28 | **Commits**: 30

## Overview

Flutter mobile app (`rhodey_app/`) built as the primary frontend for the Rhodey OS. Firebase-integrated with FCM push notifications, TTS, voice input, and in-app updates.

## Project Structure

```
rhodey_app/lib/
├── main.dart                  # App entry, Firebase init, notification routing
├── models/
│   ├── briefing.dart          # BriefingResponse model
│   ├── capture_item.dart      # Pending capture items
│   ├── decision_item.dart     # Decision pulse items
│   ├── message.dart           # Telegram-style messages
│   └── today_data.dart        # Today screen data
├── screens/
│   ├── adaptive_home_screen.dart   # Platform-adaptive landing
│   ├── dump_screen.dart            # Raw dump viewer
│   ├── history_screen.dart         # Conversation history
│   ├── inbox_screen.dart           # Message inbox
│   ├── menu_sheet.dart             # Bottom sheet menu
│   ├── rhodey_surface.dart         # Rhodey Surface v3 (Horizon/Traces)
│   ├── surface_prototype.dart      # Prototype for v1-v2
│   ├── talk_screen.dart            # Voice + TTS interaction
│   └── today_screen.dart           # Task/trace/conversation search
├── services/
│   ├── api_config.dart             # API base URL config
│   ├── api_service.dart            # HTTP client for backend
│   ├── notification_service.dart   # FCM push handling
│   └── update_service.dart         # In-app update system
├── theme/
│   └── app_theme.dart              # Warm stone palette, editorial typography
├── utils/
│   └── home_instrumentation.dart   # Performance tracking
└── widgets/
    ├── chat_bubble.dart            # Message bubbles
    ├── decision_card.dart          # Decision pulse cards
    ├── rich_card_content.dart      # Rich content rendering
    └── voice_states.dart           # Voice recording UI states
```

## Key Features

### Firebase Integration
- `firebase_options.dart` + `google-services.json`
- FCM push notification registration and handling
- `onPushReceived` triggers immediate briefing fetch on foreground push

### In-App Update System (`update_service.dart`)
- Version check against GitHub Releases
- Download APK from release assets
- Install via platform channel
- Digital signatures for update verification
- Version comparison from release title

### TTS & Voice (`talk_screen.dart`)
- Text-to-speech for Rhodey responses
- Voice mic button on home screen
- Speech recognition via `RECORD_AUDIO` permission
- Voice states UI with recording animation

### Rhodey Surface v3 (`rhodey_surface.dart`)
- Horizon/Traces design: editorial typography, warm stone palette
- Dynamic feed from `/api/briefing`
- Task/trace/conversation search via `today_screen.dart`

### Push Notifications
- FCM fire-and-forget on every `send_telegram()`
- `BriefingResponse` model with `latest_response` field
- Diagnostic endpoints: `/api/briefing-ping`, `/api/briefing-debug`

## Build Pipeline

`.github/workflows/flutter-distribute.yml`:
- Automated APK signing and version bump from `pubspec.yaml`
- `contents:write` permission for GitHub Releases upload
- `build_apk.sh` for local builds

## Key Files

| File | Purpose |
|------|---------|
| `rhodey_app/lib/main.dart` | App entry point, notifications |
| `rhodey_app/lib/services/update_service.dart` | In-app update |
| `rhodey_app/lib/services/notification_service.dart` | FCM handling |
| `rhodey_app/lib/screens/rhodey_surface.dart` | Home screen v3 |
| `rhodey_app/lib/screens/talk_screen.dart` | Voice/TTS |
| `rhodey_app/lib/screens/today_screen.dart` | Search interface |
| `rhodey_app/theme/app_theme.dart` | Design system |

## Related Docs

- [Push Notifications](38-push-notifications.md)
- [APK Versioning](43-apk-versioning.md)
- [Rhodey Surface UX](49-rhodey-surface-ux.md)
