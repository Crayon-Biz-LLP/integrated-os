# Rhodey App V2 — Adaptive Home Screen Redesign

## Core Philosophy

One screen. One scroll. Rhodey decides what you need to see, when you need to see it.

No tabs. No routing. The app opens to a single adaptive surface that shifts based on time, context, and what's pending. Every piece of data (tasks, calendar, approvals, captures, memories) appears *in the conversation* or as a contextual card — never in a separate tab you have to navigate to.

## Screen Architecture: 3 Zones, 1 Scroll

```
┌───────────────────────────────────────────┐
│  ☰  Tue, 9:41 AM                [📥 2]  │ ← Header: menu + inbox badge
├───────────────────────────────────────────┤
│                                           │
│  NOW                                      │ ← Lane 1: max 3 cards
│  ─────────────────────────────────────    │
│  ✅ Person: Sunju (Ashraya)   [↩ Undo]   │
│  📋 Send pricing to Marcus   [✓ Done]    │
│  +2 more in Inbox ▸                      │
│                                           │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   │
│                                           │
│  CONVERSATION                             │ ← Lane 2: active thread
│  ─────────────────────────────────────    │
│  ☀️ Morning. You have 3 tasks today.     │
│     Marcus @ 3pm. ✓ Task saved.           │
│                                           │
│  🎤 Remind me to call Sunju              │
│     ✓ Task saved                          │
│                                           │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   │
│                                           │
│  > Show 12 earlier messages ▴             │ ← Lane 3: collapsed
│                                           │
│  ┌────────────────────────┐ [🎤]         │
│  │  Type a message...      │              │
│  └────────────────────────┘              │
└───────────────────────────────────────────┘
```

## Zone Rules (Strict)

| Rule | Zone | Detail |
|---|---|---|
| **Content gate** | NOW | Only pending, actionable, reversible, or time-sensitive objects. No FYIs, no confirmations, no history. |
| **Capacity** | NOW | Hard limit of 3 cards. Overflow → single line: "+2 more pending in Inbox ▸" |
| **Auto-clear** | NOW | Resolved cards (approved, completed, dismissed) animate out immediately. Do not move to CONVERSATION or HISTORY. |
| **Dedup** | NOW / CONVERSATION | Object-level dedup. If a decision is approved and becomes a nudge about the same task, only one representation exists at a time — not both. |
| **Active thread** | CONVERSATION | One briefing/answer thread at a time. New user message → thread continues. |
| **Compact confirmations** | CONVERSATION | "✓ Task saved" auto-collapses to a single subtle line after 5 seconds. Does not push other content. |
| **Default state** | HISTORY | Collapsed behind "Show N earlier messages ▴" pill. **Never** auto-expanded on app open. |
| **Resolved items in history** | HISTORY | Resolved NOW items do NOT appear here unless the action has audit visibility (e.g. reversed decision). Otherwise gone for good. |

## Zone Ownership (Deterministic)

| Object Type | Zone | Why |
|---|---|---|
| Pending decision (person approval, edge, email task) | NOW | Actionable, reversible, time-sensitive |
| Overdue or due-soon task | NOW | Time-sensitive |
| Active nudge (upcoming meeting, task reminder) | NOW | Time-sensitive |
| Morning briefing | CONVERSATION | Informational, not actionable |
| User message + Rhodey response | CONVERSATION | Active conversation thread |
| "✓ Task saved" confirmation | CONVERSATION | Compact, auto-collapses |
| Rhodey answers to queries | CONVERSATION | Active conversation |
| Past messages >5 min old | HISTORY | Not active, scroll back |
| Resolved decisions/tasks | **Neither** | Gone from feed. Accessible via Inbox/Tasks in menu. |

## Feed Rules

| # | Rule |
|---|---|
| 1 | Max 1 morning briefing card |
| 2 | Max 1 active nudge at a time |
| 3 | Confirmations auto-collapse to single-line after 5s |
| 4 | Resolved cards leave the home feed entirely |
| 5 | No duplicate object representations (object-level, not text-level) |
| 6 | NOW has hard capacity (3 max) |
| 7 | Resolved NOW items do NOT reappear in HISTORY |
| 8 | Empty NOW must be explicit: "✨ All caught up" |
| 9 | HISTORY never auto-expands on app open |

## Persistent Control: Inbox Badge

- Position: **top-right of the header bar**
- Shows count: `[📥 N]`
- Tappable → opens the **Inbox screen** (pending decisions list)
- Stable position — never moves, never overlaps with input bar or gestures

## Menu Sheet Contents

Accessed via ☰ top-left. Each item opens a native Flutter page (not webview):

| Item | Page |
|---|---|
| 📋 Today | Compact agenda — tasks, calendar, captures |
| 📥 Captures | Raw capture timeline |
| 📥 Inbox | Pending decisions (same as badge destination) |
| 📜 History | Full message history |
| ⚙️ Settings | API config, TTS, notifications |
| *(future)* Tasks, Calendar, People, Memories, Emails, Patterns |

## Instrumentation

Local counters logged to debug console:
- `nowCardsShown` — number of NOW cards rendered
- `inboxBadgeTaps` — badge tap count
- `menuOpens` — menu sheet opens
- `homeActionsCompleted` — tasks done / decisions approved from home
- `dedupSuppressions` — objects blocked from duplicate display
- `itemsDismissed` — swipe-away or "not now" without action

## Build Phases

| Phase | What | Status |
|---|---|---|
| **P1** | Adaptive skeleton: 3-zone layout, header, menu sheet, NOW with real data, CONVERSATION, HISTORY collapsed, feature flag | ✅ |
| P2 | NOW zone improvements: capacity limit enforcement, dedup, auto-clear | ⏳ |
| P3 | Rich inline cards: task cards with buttons, decision cards in CONVERSATION | ⏳ |
| P4 | Proactive push notifications (briefings, nudges, approvals via FCM) | ⏳ |
| P5 | TTS on tap (tap any Rhodey bubble → reads aloud) | ⏳ |
| P6 | Native menu pages (Tasks, Calendar, People, Memories, etc.) | ⏳ |

## Rollback

Feature flag in `main.dart`:
```dart
static const bool useNewHome = bool.fromEnvironment('USE_NEW_HOME', defaultValue: true);
```

When `false`, the old 4-tab `MainShell` renders unchanged. All tab code is preserved.
