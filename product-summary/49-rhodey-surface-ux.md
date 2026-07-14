# Rhodey Surface UX

**Date**: Jul 10-11, 2026 | **Phase**: 27 | **Commits**: ~8

## Evolution

### v1 — Card-Based Feed (Jul 10)
First Flutter home screen redesign. Fetched data from `/api/briefing` and rendered it as cards. Each briefing section became a card with title, content, and metadata.

### v2 — Briefing-Based Sections (Jul 10)
Restructured the home screen to match the briefing schema. Sections for tasks, calendar events, memories, and notifications. Added section headers and visual hierarchy.

### v3 — Horizon/Traces (Jul 10-11)
Final design direction with three pillars:
- **Editorial typography**: Serif headlines for sections, clean sans-serif for content, generous leading
- **Warm stone palette**: Earth tones (warm grays, terracotta accents, cream backgrounds) — defined in `app_theme.dart`
- **Traces**: A search-first interface for finding tasks, conversations, and memories

## Key Screens

### `rhodey_surface.dart`
The main home screen. Dynamic feed that adapts to what's available:
- Morning briefing cards
- Pending decision items (approve/reject)
- Recent conversation snippets
- Quick action buttons

### `today_screen.dart`
Tab-based search interface:
- **Tasks tab**: Filterable task list with status indicators
- **Traces tab**: Search recent conversations by content
- **Conversations tab**: Browse recent threads

### `surface_prototype.dart`
Prototype scratchpad used during v1-v2 development.

## Design System

Defined in `app_theme.dart`:
- Color palette: warm neutrals, muted earth tones
- Typography: serif for headlines, system sans-serif for body
- Spacing: relaxed with generous padding
- Dark mode: muted with warm undertones

## App Redesign v2 (Predecessor, Jul 8)

Five-phase redesign of the pre-Surface app:
- **P1**: Notification refactor — cleaner push handling
- **P2**: Conversation list — threaded message views
- **P3**: Individual conversation view — full chat UI
- **P4**: Decoration polish — visual consistency pass
- **P5**: Sound/vibration on notification

Task-or-note popup dialog removed — bot responses send directly to the app screen.

## Key Files

| File | Purpose |
|------|---------|
| `rhodey_app/lib/screens/rhodey_surface.dart` | Home screen v3 |
| `rhodey_app/lib/screens/today_screen.dart` | Search interface |
| `rhodey_app/lib/screens/surface_prototype.dart` | v1-v2 prototype |
| `rhodey_app/theme/app_theme.dart` | Design tokens |
| `rhodey_app/lib/main.dart` | P1-P5 redesign |

## Related Docs

- [Flutter App Architecture](48-flutter-app-architecture.md)
- [Push Notifications](38-push-notifications.md)
- [APK Versioning](43-apk-versioning.md)
