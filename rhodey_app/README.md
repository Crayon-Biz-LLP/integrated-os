# rhodey_app

The Flutter client for Integrated-OS — Rhodey, your Chief of Staff in your pocket.

## What it does

- **Onboarding** — sign-in via Google / email-OTP (no API-key pasting), persona setup, and a "how Rhodey works" primer.
- **Home modes** — the front door adapts to where you are in your day: proceed / decide / sprint / catch-up / wrap.
- **Inbox & approvals** — quick confirmations with type filtering, selection-mode batch approve/reject, channel batch approve, and **per-item undo** (side-effects reversed via the `undo_*` decision flow).
- **Today / history / entities** — the day's plan, past decisions and audit, and the knowledge-graph entities Rhodey knows about.
- **Voice & quick capture** — a Telegram-independent reply path: Rhodey can hear and render acknowledgments without needing Telegram at all.
- **Settings** — persona, notification, and account management.

## Layout

```
lib/
  main.dart          # entry point
  screens/           # inbox, today, history, entities, adaptive_home, onboarding/, settings, ...
  services/          # API client, auth, push, briefing
  models/            # typed models for tasks, decisions, briefings, entities
  widgets/           # chat bubbles, decision cards, mode switchers
  voice/             # speech capture + rendering
  theme/             # app theming
```

## Testing

```bash
flutter test                                        # unit + widget tests
flutter test integration_test -d <device>           # on-device E2E (onboarding flow)
```

The full app suite (including the on-device integration tests) is wired into the repo's test gate — see `tests/README.md` and `plans/75-comprehensive-test-plan.md`.
