# 71 — Onboarding "Try it now" demo (M10)

## Why

The onboarding journey teaches WHAT Rhodey is (briefings, board, approvals) but
never HOW to talk to it. New tenants type at the chat like a search box or stay
silent — the intent model (TASK/COMPLETION/NOTE/QUERY) is the core magic and it
is invisible until you try it. A hands-on demo during onboarding is what makes
every feature discoverable; without it the OS's intelligence is inert for a
new user.

User decisions (confirmed):
- **Real, marked as demo** — demo actions genuinely create tasks/notes in the
  tenant's world via the real pipeline, tagged `[onboarding-demo]`, with a
  cleanup action. Real "aha" (item appears on their board).
- **Full spectrum** — chat magic (task / completion / note / query), boards
  tour (Today/Inbox/Entities/History), voice capture try, quick-reply demo.

## Key architecture decision

The seed MUST happen BEFORE the demo. A QUERY demo ("who's in my world?") needs
the graph to exist; a COMPLETION demo needs the TASK demo to have created a
task first. Current flow seeds at the very end (step 9 "Create my world") —
so the seed step moves up, immediately after the collection steps
(about/people/plate/areas/times). The final "first briefing" then reflects the
real seeded world + demo items.

## New flow (was 10 steps, now 11)

1. Welcome
2. Your key (validate)
3. Who are you? (context)
4. Your people
5. Your plate (tasks)
6. Areas
7. Briefing times (presets) + timezone
8. **Create my world** (seed NOW — shows the "Your world, as I see it" summary)
9. **Try it now — chat** (NEW: 4 guided real actions, tap-to-try)
10. **Your surfaces** (NEW: Today/Inbox/Entities/History tour, mini previews)
11. **Voice + quick replies** (NEW: hold-to-talk try; quick-reply demo)
12. Google (optional) → first briefing → Enter

## Demo script (chat step) — real messages through the real pipeline

Each is a tappable card ("Try it") that sends the message via a NEW endpoint
`/api/demo/message` and renders the reply + intent tag:

1. **TASK** — "Remind me to call Meera about the hiring plan tomorrow morning"
   → shows 📋 Task created; task appears on their board.
2. **COMPLETION** — "The Meera call is done" → ✅ Task closed (completes the
   task from #1 — proves statefulness).
3. **NOTE** — "Meera said the offer letter is out" → 📝 Noted, linked to
   Meera's person node (proves people linking).
4. **QUERY** — "Who is in my world?" → ❓ answered from THEIR seeded graph
   (proves retrieval + personalization).

Each demo card shows: the example message, what to expect, and after tapping —
the intent tag + a one-line real result. Demo actions stamp
`[onboarding-demo]` so they're identifiable and cleanable.

## Backend

- `POST /api/demo/message` — same auth as send-message; runs the REAL
  `_run_web_message_pipeline` inline (not the background Modal spawn, so the
  reply is synchronous for the demo), then stamps:
  - raw_dumps inbound/outbound rows: `metadata.demo = true`
  - tasks created by the demo message: `notes` suffixed `[onboarding-demo]`
  - memories created: `metadata.demo = true`
- `POST /api/demo/cleanup` — deletes the tenant's demo-tagged raw_dumps rows,
  demo tasks (only those still `todo`), leaves graph nodes (they're pending
  approvals — the Inbox is the designed flow; rejected there is the training
  signal). Returns counts. Idempotent.
- Guard: demo endpoints refuse once `onboarding_state == 'seeded'` AND demo
  already completed (idempotent journey); demo stamping keys off the exact
  scripted messages so we never tag real user messages.

## App

- `onboarding_flow.dart`: reorder steps; add `_demoChatStep()`,
  `_surfacesStep()`, `_voiceStep()`; seed at step 8 (reuse `_complete()`
  without the final page jump); "Try it" cards call the demo endpoint.
- Demo step shows a live mini-chat (bubble list) that the tap-to-try cards
  feed; intent tag rendered from the response.
- Surfaces step: 4 compact cards (Today/Inbox/Entities/History) with what
  lands where — static, honest previews.
- Voice step: reuse QuickCaptureOverlay's speech-to-text; the transcribed
  demo phrase runs through the same demo message path.
- Cleanup: after demo, "Keep demo items" (default) vs "Clear them"; also
  reachable from Settings later.

## Validation

- Backend: py_compile + ruff + pytest (tests/unit).
- App: `flutter analyze` in rhodey_app if toolchain available.
- Manual: dry-run demo message against live for a scratch tenant, confirm
  tagging + cleanup counts.
