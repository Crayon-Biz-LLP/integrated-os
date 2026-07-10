# Rhodey OS — Complete Product Brief for Mobile App Design

## Note for Lovable

This document describes a complete AI-powered Personal Operating System called **Rhodey OS** (codename: Integrated-OS). Your task is to design and build the **mobile companion app** (Flutter) that serves as the primary interface for this system. The app is NOT a chatbot. It is a **living briefing surface** — an executive assistant's dashboard that feels aware, structured, and calm. Read the entire document carefully to understand the philosophy before writing any code.

---

## 1. Product Identity

### What Is Rhodey OS?

Rhodey OS is an AI-powered Executive Command Center for a single person's life. It bridges raw input (voice notes, text messages, emails, journal entries, photos) with strategic execution (tasks, calendar events, knowledge graphs, briefings). It manages work across **7 life domains**: a technology services company (SOLVSTRAT), a product company (QHORD), a governance/legal entity (CRAYON), incubator projects, church administration (ASHRAYA), family life, and personal health/spirituality.

This is NOT a SaaS product. It is a hyper-personalized, bespoke system built for one person. There are no user accounts, no multi-tenancy, no login screens. It is single-user by design.

### The Core Loop

The system operates as a triangular engine:

1. **Intake** — Voice notes, text messages, emails, journal entries, photos, and documents arrive via Telegram, Gmail, Outlook, Google Forms, and the mobile app itself
2. **Intelligence** — A Gemini AI layer classifies intent, extracts entities, searches memories, traverses a knowledge graph, and generates briefings
3. **Execution** — Tasks are created in Google Calendar + Google Tasks, people are added to the knowledge graph, decisions are queued for approval, and briefings are delivered

### Key Differentiators

- **Not a chatbot**: The app does NOT show a transcript of past conversations. It shows a structured briefing of what matters right now
- **Not a dashboard**: There are no decorative charts, no empty state placeholders, no widget grids. Every pixel serves awareness or action
- **Hyper-personalized**: The system knows the user's strategic season, their relationships, their projects, their habits
- **Zero infrastructure cost**: Runs on free tiers of Vercel, Supabase, GitHub Actions, and Gemini API
- **Self-healing**: 313+ error guards, zombie recovery, dead letter queues, triple LLM fallback chain

---

## 2. Core Philosophy: The Anti-Chatbot Manifesto

### What Makes Something Feel Like a Chatbot?

Six patterns, any one of which makes an app feel like a chatbot:

1. **The center of the screen is a transcript** — A chronological list of user messages and bot responses. This is the defining pattern of every chat app ever made.
2. **The input bar dominates** — A large text field at the bottom with a send button. The primary interaction is typing.
3. **Every action requires typing** — You can't approve, dismiss, or act without composing a message.
4. **The app waits for you to initiate** — Nothing happens until you type something. The app is reactive, not proactive.
5. **State is hidden until you ask for it** — Your tasks, calendar, decisions — all invisible unless you explicitly query them.
6. **The interface is symmetrical** — User messages and bot responses have equal visual weight, creating the illusion of two equal participants in a conversation.

### Rhodey's Design Principles

Rhodey breaks every one of these patterns:

1. **Center is a briefing, not a transcript** — The home screen shows three structured sections: what's relevant now (calendar + tasks), what needs your decision (pending items), and what just happened (recent outcomes). No alternating user/bot messages.
2. **Input is subordinate** — The mic and keyboard are in a minimal dock. Voice is the primary input (tap to speak), typing is secondary. The input doesn't dominate the layout.
3. **Most actions don't require typing** — Approve a decision, dismiss a suggestion, accept a merge — all with a single tap on an inline chip. No composition needed.
4. **The surface is proactive** — It shows upcoming events, urgent tasks, and pending decisions without being asked. It updates every 10 seconds via polling.
5. **State is always visible** — The briefing IS the state. Tasks, calendar, decisions, recent outcomes — all on the surface.
6. **Asymmetric by design** — The user's input vanishes into outcomes. You speak, the briefing changes. There is no "you said" / "Rhodey said" alternation. The user's messages are not preserved on the surface — they're ephemeral triggers that produce visible state changes.

### The Surface Metaphor

Think of Rhodey as a physical desk, not a chat log. Rhodey's voice is center, primary, full weight. Your inputs are marginalia — actions that cause the surface to update, not items to be archived. The metaphor is: **Rhodey owns the surface. You write in the outcomes.**

---

## 3. Architecture Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     INTAKE                               │
│  Mobile App │ Telegram │ Gmail/Outlook │ Google Forms    │
│  Voice / Text / Email / Journal / Photos                 │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│                   INTELLIGENCE                           │
│  Intent Classification (Gemini)  │  Memory (Vector DB)   │
│  Entity Routing                  │  Knowledge Graph      │
│  Hybrid Search (Vector + Graph)  │  Serendipity Engine   │
│  Canonical Pages (Brain Synth)   │  Practice Detection   │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│                     EXECUTION                            │
│  Task Creation │ Google Calendar │ Google Tasks          │
│  Project/People Auto-Creation   │ Graph Edge Creation    │
│  Briefing Generation            │ Decision Queue         │
└─────────────────────────────────────────────────────────┘
```

### Technology Stack

| Layer | Technology |
|-------|-----------|
| Backend API | Python FastAPI on Vercel (serverless) |
| Database | Supabase (PostgreSQL + pgvector) |
| AI | Gemini 3 Flash, Gemini Flash Lite, Gemini Embedding 2 |
| Knowledge Graph | Custom graph_edges + graph_nodes tables |
| Calendar | Google Calendar API + Microsoft Graph API |
| Tasks | Google Tasks API |
| Messaging | Telegram Bot API |
| Mobile App | **Flutter** (this is what you're building) |

### Key API Endpoints (Mobile App Communicates With)

| Endpoint | Purpose | Response |
|----------|---------|----------|
| `GET /api/briefing` | Fetch the home surface briefing | Structured sections (greeting, briefing, decisions, recent) |
| `POST /api/send-message` | Send voice/text input | Response text + updated briefing |
| `GET /api/tasks` | Active tasks list | JSON array with deadlines, priorities |
| `GET /api/calendar-events` | Today's calendar events | JSON array with start/end times |
| `GET /api/messages` | Message history | Chronological log (for menu → history screen) |
| `PATCH /api/tasks/{id}/status` | Mark task done | Updated task object |
| `POST /api/graph-node-action` | Approve/reject graph node | Success/error |
| `POST /api/graph-edge-action` | Approve/reject graph edge | Success/error |
| `POST /api/email-action` | Approve/reject email task | Success/error |
| `POST /api/whatsapp-action` | Approve/reject WhatsApp item | Success/error |
| `POST /api/call-action` | Approve/reject call item | Success/error |
| `GET /api/app-version` | Check for app updates | Version info + download URL |

### The Briefing API Response (Most Important)

```json
{
  "greeting": "Good morning, Danny. Qhord sync at 19:30.",
  "next_event": "Qhord sync at 19:30",
  "sections": [
    {
      "id": "briefing",
      "title": "Your evening",
      "items": [
        {"icon": "⏰", "text": "Send pricing deck — due in 4h", "status": "urgent"},
        {"icon": "📅", "text": "Qhord sync at 19:30", "status": "active"},
        {"icon": "📝", "text": "Call Sunju re school", "status": "active"}
      ]
    },
    {
      "id": "decisions",
      "title": "Decisions",
      "items": [
        {"icon": "🔗", "text": "Add \"Anjali\" as person?", "status": "pending",
         "decision_id": "42", "decision_type": "graph_node"},
        {"icon": "🔀", "text": "Merge: \"Armour Tech\" → \"Armour Cyber\"?", "status": "pending",
         "decision_id": "43", "decision_type": "merge"}
      ]
    },
    {
      "id": "recent",
      "title": "Recent",
      "items": [
        {"icon": "✅", "text": "Done: Send pricing deck by Thu", "status": "done"},
        {"icon": "📝", "text": "Noted: 90-day payment cycle", "status": "note"}
      ]
    }
  ],
  "pending_count": 2
}
```

Key rules:
- **Decisions section is omitted entirely** when there are no pending items. Never show an empty section.
- **Recent section is capped at 3 items maximum**. Never exceed this.
- **Items carry `decision_id` + `decision_type`** for inline action chips.
- **`greeting` includes the next calendar event** when available.

### The Send-Message Response

The same endpoint also returns an updated briefing after processing:

```json
{
  "success": true,
  "response": "✅ Task created: Send pricing deck by Thursday",
  "briefing_update": { /* same structure as GET /api/briefing */ }
}
```

The frontend should:
1. Show the `response` text as a transient overlay (2-3 seconds)
2. Replace the current briefing with `briefing_update` immediately
3. Fade the overlay once the briefing is visibly updated

---

## 4. Mobile App Screen-by-Screen Specification

### Screen 1: Home Surface (The Briefing)

This is THE screen. Everything lives here. It should never feel like a chat.

#### Layout (top to bottom):

```
┌──────────────────────────────────────┐
│ ● Rhodey                  [Listening]│  ← Presence strip. 44px. Fixed.
│                                      │
│ Good morning, Danny.                 │  ← Greeting. 17px, semibold.
│ Qhord sync at 19:30.                 │  ← Next event. 13px, blue.
│                        [2 pending]   │  ← Badge (amber). Only if >0.
│                                      │
│ YOUR EVENING                         │  ← Section title. 11px, uppercase.
│ ┌──────────────────────────────────┐ │
│ │ ⏰ Send pricing deck — due in 4h │ │  ← Urgent items in red
│ │ 📅 Qhord sync at 19:30          │ │  ← Active items in white
│ │ 📝 Call Sunju re school          │ │
│ └──────────────────────────────────┘ │
│                                      │
│ DECISIONS                            │  ← CONDITIONAL: only shown if >0 items
│ ┌──────────────────────────────────┐ │
│ │ 🔗 Add "Anjali" as person?       │ │
│ │    [Approve] [Dismiss]           │ │  ← Inline action chips
│ │ 🔀 Merge: "Armour Tech" → ...    │ │
│ │    [Accept] [Reject]             │ │
│ └──────────────────────────────────┘ │
│                                      │
│ RECENT                               │  ← Max 3 items, hard cap
│ ┌──────────────────────────────────┐ │
│ │ ✅ Done: Send pricing deck by Thu│ │
│ │ 📝 Noted: 90-day payment cycle   │ │
│ └──────────────────────────────────┘ │
│                                      │
│ ─────────────────────────────────── │
│ [≡]            🎤 Tap to speak       │  ← Bottom dock. 56px. Fixed.
└──────────────────────────────────────┘
```

#### Design Details:

**Presence Strip:**
- Green pulsing dot (always animated, calm pulse)
- "Rhodey" label in muted grey
- When listening: 3-bar audio visualizer appears next to the dot

**Greeting:**
- Large (17px), semibold, white
- Based on time of day: "Good morning / afternoon / evening, [Name]."
- Next calendar event shown in blue (13px) beneath, if available
- If pending decisions exist, an amber badge shows count in top-right

**Sections:**
- Each section has an uppercase title (11px, grey, 0.8 letter spacing)
- Items are rows with icon + text + optional action chips
- Icons on the left (14px), text beside them (13px)
- Urgent items use red text + subtle red background tint
- Items have 10px rounded corners, subtle dark card background
- No dividers between items — spacing does the work

**Decision Action Chips:**
- Green "Approve" / "Accept" chip on the left
- Grey "Dismiss" / "Reject" chip on the right
- Wrapped in a Row with 8px spacing
- On tap: show brief loading state, call appropriate API endpoint, re-fetch briefing
- Chips should be small (12px font, 7px vertical padding, 14px horizontal)

**Bottom Dock (Default State):**
- Three elements equally spaced: Menu icon (left), Mic button (center, prominent), Keyboard icon (right)
- Menu icon has an amber notification dot (7px) when pending_count > 0
- Mic button is the primary CTA: "🎤 Tap to speak" with a subtle border
- When listening: button turns green, text changes to "🎤 Listening..."
- Dark background (#161618), subtle top border

**Bottom Dock (Keyboard Active):**
- Text field replaces the dock (standard send UI)
- Text field has dark background with subtle border, rounded corners
- Send button (blue) + Close button (grey) on the right
- On submit: send message, close keyboard, show response moment

#### States:

**Loading State:** Centered spinner (small, grey). No skeleton — just a clean loading indicator.

**Empty/Error State:**
```
Hey, I'm your companion.
To start, just speak or type whatever's on your mind.

📝 "Remind me to call Sunju"
🗣️ "What's new today?"
📝 "Note down an idea"

(nothing yet — your surface will fill as we talk)
[Retry] (only if API error)
```

- Three starter chips (tappable suggestions)
- Grey italic hint text
- Retry button appears ONLY on actual API failure, not on genuine empty state

**Transient Response Moment:**
- After sending a message: a floating card appears at center-bottom of screen
- Card has: green left accent bar, response text, subtle border + shadow
- Fades in quickly (400ms), stays minimum 2 seconds
- Briefing updates underneath the overlay
- After 2 seconds (or when briefing is confirmed updated), fades out
- If user sends another message while moment is visible: old moment is replaced, timer resets

**Pull-to-Refresh:**
- Pull down to manually refresh the briefing
- Shows a brief loading indicator at top

### Screen 2: Menu / Settings Drawer (Bottom Sheet)

Triggered by tapping the menu icon. Not a separate screen — a modal bottom sheet.

**Content:**
- **History** — Opens a full-screen message history view (chronological log of all interactions, for reference only)
- **Today** — Opens the task list / calendar view
- **Decisions** — Opens the pending decisions list (if count > 0, shows badge)
- **Settings** — API endpoint configuration, app version info, preferences
- **About** — App version, build info

The menu sheet should feel like iOS settings — clean rows with icons, no decoration.

### Screen 3: History (Full-screen, Navigation-pushed)

A chronological log of past messages and responses. Uses the same card-based format from the original design, but as a secondary screen (not the home surface).

- Reverse-chronological (newest at bottom, auto-scroll)
- Each item shows timestamp, direction (incoming/outgoing), content
- Simple, utilitarian, no chat bubbles
- This is the ONLY place where user messages are visible permanently
- Tapping an item copies it to clipboard

### Screen 4: Today / Tasks (Full-screen, Navigation-pushed)

A focused view of today's tasks and calendar events.

- Tasks grouped by urgency: Overdue → Due Today → Upcoming
- Calendar events shown as timeline blocks
- Each task has a checkbox to mark as done
- Tap a task to see details (project, deadline, notes)
- Pull to refresh

### Screen 5: Decisions Inbox (Full-screen, Navigation-pushed)

All pending decisions in a flat list. Same structure as the Decisions section on the home surface, but comprehensive (not limited).

- Grouped by type: Graph Nodes → Graph Edges → Email → WhatsApp → Calls
- Each item has the same inline action chips (Approve/Dismiss)
- Badge in the AppBar showing total count
- Pull to refresh

---

## 5. Interaction Patterns

### Speaking (Primary Input)

1. User taps "🎤 Tap to speak"
2. Green pulsing indicator appears in presence strip
3. 3-bar audio visualizer animates
4. Speech recognition runs (15-second timeout)
5. On result: indicator disappears, message sent
6. Response moment appears with Rhodey's spoken confirmation
7. TTS reads the response aloud
8. Briefing updates underneath
9. Response moment fades

### Typing (Secondary Input)

1. User taps keyboard icon
2. Bottom dock transitions to text field
3. User types and submits (send button or Enter)
4. Keyboard dismisses, dock returns to default
5. Same response moment + briefing update flow

### Approving a Decision

1. User taps "Approve" chip on a decision item
2. Response moment shows "✅ approve..." briefly
3. API call fires (e.g., POST /api/graph-node-action)
4. On success: briefing re-fetches, decision item disappears
5. Response moment shows confirmation, then fades
6. On failure: response moment shows error, item stays visible

### Notification Handling

- Push notifications arrive for: pending decisions, upcoming events, new briefings
- Tap notification → deep link to relevant screen:
  - Decision notification → Decisions Inbox screen
  - Event notification → Today screen
  - Briefing notification → Home Surface (already there)

---

## 6. Visual Design System

### Color Palette

| Token | Hex | Usage |
|-------|-----|-------|
| Surface Background | `#0E0E10` | Main app background |
| Card Background | `#161618` | Item row backgrounds |
| Primary Text | `#F2F2F2` | Main body text |
| Muted Text | `#6B6B70` | Secondary text, labels |
| Accent Green | `#34C759` | Success, presence dot, approve actions |
| Accent Amber | `#FFD60A` | Warning, pending indicator |
| Accent Blue | `#007AFF` | Active items, links |
| Accent Red | `#EF5350` | Urgent/overdue items |
| Card Border | `#2C2C30` | Subtle borders between elements |
| Section Title | `#8E8E93` | Section header text |

### Typography

- **Greeting**: 17px, semibold (w500)
- **Section title**: 11px, semibold (w600), uppercase, 0.8 letter-spacing
- **Item text**: 13px, regular (w400), 1.4 line-height
- **Urgent item**: 13px, medium (w500), red
- **Chip label**: 12px, medium (w500)
- **Presence label**: 12px, medium (w500)
- **Hint text**: 11px, italic

### Spacing

- Horizontal padding: 16px (items), 20px (section titles)
- Between items: 2px vertical
- Between sections: 16px vertical
- Card internal padding: 12px horizontal, 10px vertical
- Icon to text gap: 10px

### Corner Radii

- Item rows: 10px
- Response moment card: 14px
- Action chips: 8px
- Starter chips: 12px

### Dark Mode Only

The app is dark mode only. No light mode. The background is pure black (`#0E0E10`), not dark grey, to blend with OLED phone bezels.

### Animation

- **Presence dot pulse**: 2s ease-in-out, opacity 0.4→1.0, repeat
- **Response moment**: 400ms fade in, 400ms fade out
- **Item appear**: subtle opacity + translateY (250ms)
- **Listening indicator**: 1.2s bar animation, repeat
- **Section transitions**: no animation (content replaces in place)

---

## 7. Feature Summary (What to Build)

### Must Have (MVP)
- [x] Home briefing surface with 3 sections (greeting, briefing, decisions, recent)
- [x] Voice input with speech recognition + TTS output
- [x] Text input via keyboard
- [x] Inline decision action chips (approve/dismiss on graph nodes, edges, emails, WhatsApp, calls)
- [x] Response moment overlay after sending input
- [x] Auto-refresh briefing every 10 seconds
- [x] Pull-to-refresh
- [x] Menu bottom sheet (history link, today link, decisions link, settings)
- [x] Push notification handling with deep links
- [x] App update check (dialog when newer version available)
- [x] Blank/empty state with starter chips
- [x] Error state with retry button

### Should Have (Soon After)
- [ ] History screen (chronological log of past interactions)
- [ ] Today screen (focused task + calendar view)
- [ ] Decisions inbox screen (comprehensive pending decisions)
- [ ] Settings screen (API config, preferences)
- [ ] In-app download + install for app updates

### Nice to Have (Future)
- [ ] Haptic feedback on voice send
- [ ] Voice activity detection (auto-stop recording on silence)
- [ ] Offline mode (cached briefing)
- [ ] Widget support (home screen briefing widget)
- [ ] Wear OS companion

---

## 8. Critical "Don't Do" List

These are hard rules. Violating any of them will make the app feel like a chatbot:

1. **DON'T** show alternating user/bot messages on the home surface. User input vanishes into the briefing.
2. **DON'T** put a large text input at the bottom. Voice is primary, keyboard is secondary. The dock is minimal.
3. **DON'T** require typing for common actions. Approve, dismiss, mark done — all via taps.
4. **DON'T** show an empty Decisions section. If there are no pending items, the section is completely hidden.
5. **DON'T** show more than 3 items in the Recent section. Hard cap.
6. **DON'T** use chat bubbles (left/right alignment). Everything is flush left.
7. **DON'T** show timestamps on individual items. The section titles provide temporal context.
8. **DON'T** make the app feel reactive. It should show state proactively on open.
9. **DON'T** use decorative dashboard patterns (charts, gauges, progress bars). Every pixel has a job.
10. **DON'T** show the user's past messages as permanent cards. They're ephemeral triggers.

---

## 9. Data Flow Summary

### On App Open:
```
GET /api/briefing → Render greeting + sections
  ├── Briefing section: tasks (urgent→active) + calendar events (next 6h)
  ├── Decisions section: pending graph nodes + edges + channel items (omitted if empty)
  └── Recent section: last 30min of outcomes (capped at 3 items)

Start 10-second polling timer
```

### On Voice/Text Input:
```
POST /api/send-message { message: "..." }
  → Server processes via Telegram webhook (LLM classification + execution)
  → Returns { response: "...", briefing_update: {...} }

Frontend:
  1. Speak response aloud via TTS
  2. Show response as floating overlay
  3. Replace briefing with briefing_update immediately
  4. After 2 seconds: fade overlay
  5. Resume polling
```

### On Decision Chip Tap:
```
PUT /api/graph-node-action { id: 42, action: "approve" }
  → Server processes the decision
  → Returns { success: true/false }

Frontend:
  1. Show brief "approve..." in response moment
  2. Re-fetch briefing
  3. Decision item disappears from section
  4. Dismiss response moment
```

---

## 10. File Structure (Proposed for Flutter App)

```
rhodey_app/
├── lib/
│   ├── main.dart                    — App entry, MainShell, update check
│   ├── theme/
│   │   └── app_theme.dart          — Color constants, text styles
│   ├── models/
│   │   ├── briefing.dart           — BriefingResponse, BriefingSection, BriefingItem
│   │   └── api_response.dart       — ApiResult<T>, PendingDecision, CalendarEventItem
│   ├── services/
│   │   ├── api_service.dart        — All HTTP calls to backend
│   │   ├── api_config.dart         — Persisted base URL + API key
│   │   ├── update_service.dart     — App update check + download
│   │   └── notification_service.dart — FCM push notification handling
│   └── screens/
│       ├── rhodey_surface.dart     — Home briefing surface (THE screen)
│       ├── menu_sheet.dart         — Bottom sheet menu
│       ├── history_screen.dart     — Message history log
│       ├── today_screen.dart       — Tasks + calendar view
│       └── inbox_screen.dart       — Comprehensive decisions list
```

---

## 11. Implementation Notes for Lovable

### The Backend Already Exists

This is IMPORTANT. The entire backend API is already built and deployed at `https://integrated-os.vercel.app`. You don't need to create any backend logic. Your job is to build the Flutter client that consumes these APIs.

### Authentication

Every API request must include an `X-API-Key` header. The API key is configured by the user in the app's Settings screen and persisted via SharedPreferences. The singleton `ApiService` handles this automatically.

### The Singleton Pattern

`ApiService` is a singleton. Do NOT create multiple instances. Use `ApiService()` (factory constructor) everywhere.

### Retry Logic

The API client has built-in retry logic for 429 (rate limit) and 5xx errors. Up to 3 attempts with jittered backoff. Mute endpoints use 15-second timeout. The send-message endpoint uses 30-second timeout (because it includes LLM processing on Vercel serverless).

### Pull-to-Refresh

After a pull-to-refresh on the home surface, call `GET /api/briefing` and replace the current briefing. Show a brief loading indicator at the top.

### Dependencies (pubspec.yaml)

```yaml
dependencies:
  flutter: sdk
  http: ^1.6.0
  shared_preferences: ^2.5.5
  speech_to_text: ^7.4.0
  flutter_tts: ^4.2.2
  firebase_messaging: ^16.4.1
  flutter_local_notifications: ^22.0.1
  package_info_plus: ^8.3.0
  open_filex: ^4.5.0
```

### State Management

Use `setState` with StatefulWidget. No need for Provider, Riverpod, or Bloc — the app is simple enough that local state is sufficient. The `_RhodeySurfaceState` holds the current `BriefingResponse` and UI state flags.

---

## Final Note

The entire product philosophy can be summarized in one sentence:

**Rhodey is not something you talk to. It's something you act through.**

The mobile app should make this feel true on every pixel. When someone opens the app, they should not see a conversation. They should see their situation — what needs attention, what's coming up, what's been decided. The input is a control channel, not a chat box. The output is a living briefing, not a response.

Build the surface. Make it calm. Make it actionable. And never, ever make it look like a chatbot.
