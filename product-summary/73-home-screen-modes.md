# 73. Home-Screen Modes & Focal Intelligence

> Verified against code 2026-08-15. This is the vision's "reduce decision
> fatigue at the front door" made concrete — the app's home surface adapts to
> *where you are in your day* instead of showing the same wall every time.

## The five modes

The Pulse computes a `home_mode` on `PulseOutput`
(`core/pulse/models.py:19-27`), which drives the Flutter home-screen layout:

| Mode | Meaning |
|------|---------|
| `proceed` | Default — steady state, nothing unusual |
| `decide` | A cluster of pending decisions needs your call |
| `sprint` | Deep-work window — heavy focus, fewer interruptions |
| `catch_up` | Backlog/vault has accumulated — surface it |
| `wrap` | End-of-day — wrap up loops, plan tomorrow |

Extraction is defensive (`core/pulse/briefing.py:1435-1443`): the raw model
output is validated against the allowed set and defaults to `proceed` on
anything unexpected.

## `app_intelligence` (the 20:00 Intel cron)

The nightly Intel pass (`core/pulse/briefing.py`, upserted into the
`app_intelligence` table) snapshots the home surface's state:

- `home_mode` / `pulse_mode` / `pulse_run_id` — which mode was computed and by
  which pulse.
- `context` / `context_bar` — the one-line situational summary for the top of
  the screen.
- **`top_focal_item`** — the LLM-chosen single focal item (what you should do
  *now*), with `overdue_list` / `stale_list` / `nag_list` behind it.
- **`vaulted_count`** — vault segmentation: how many items are parked out of
  the main view (the front door shows the focal item, not the vault).
- `insights` / `delta_snapshot` — what changed since the last pass and the
  serendipity-style insights.
- **`transparency_report`** — why the mode/focal item was chosen, so the
  recommendation is auditable (no "lie buttons").
- `voice_line` — the persona-toned line the app speaks.

## Feedback loop

The app's mode-switch feedback endpoint lets the user correct the mode; the
correction lands in the learning loop (doc 71) so Rhodey learns when *you*
think it's decision-time vs sprint-time.
