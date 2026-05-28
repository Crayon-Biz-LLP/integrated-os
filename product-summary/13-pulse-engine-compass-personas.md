# 13. The Pulse Engine — Compass Opening & Briefing Personas

## The Compass Opening

Every briefing starts with a 1-2 sentence opening that weaves journal insights into tactical reality. This is the most sophisticated prompt engineering in the system.

### The Prompt Instruction

```
THE COMPASS (OPENING SYNTHESIS): Do not create a separate section for his journal.
Instead, start the briefing with 1-2 sharp sentences that seamlessly weave his latest
HINDSIGHT insights (Faith Score, Emotional Intensity, Takeaways, or [PROPHECY])
into the current tactical reality (Qhord, Solvstrat, Debt).
```

### Stale Signal Detection

The system tracks whether hindsight data is fresh. A `HINDSIGHT_STALE` boolean is computed based on timestamp of the latest memory vs. current time:

```
If HINDSIGHT_STALE is FALSE: weave the latest hindsight insights into a sharp,
forward-leaning opening.

If HINDSIGHT_STALE is TRUE: Do NOT repeat old insights. Instead, acknowledge
the silence with a dry, one-sentence observation (e.g., 'The signal is quiet on
the reflection front, Danny. Let's look at the board.') and move immediately
to the tactical list.
```

### Temporal Lens

The compass adjusts its perspective based on time of day:
- **MORNING**: Focus on the "Delta" — what happened overnight, single most important pivot for today
- **AFTERNOON**: Focus on "Velocity" — what is actually moving (or stalled) in the last 4 hours
- **CLOSING LOOP (3:30-7pm)**: Focus on "Hand-off" — last work loop closed or closest to closing
- **NIGHT**: Focus on "Audit & Archive" — the opening should feel like a "Door Closing"

## The 5 Briefing Personas

The system shifts between 5 distinct modes based on IST time of day:

### Weekend Mode
```
Persona: Chores & Ideas
Focus: Personal tasks, family, spiritual reflections
Structure: Relaxed but structured
Weekend re-entry (Monday): Adds "🛡️ WEEKEND RECON" section
```

### Morning Mode (<12:00 IST)
```
Persona: Strategic Focus
Focus: Highest-impact work, revenue-critical tasks, new opportunities
Tone: Forward-leaning, decisive
Structure: 🔴 Urgent → 🚀 Work → 🏠 Home → ⛪ Church → 💡 Ideas
```

### Afternoon Mode (12:00-15:30 IST)
```
Persona: Execution Mode
Focus: What's actually moving, what's stalled, velocity check
Tone: Direct, no-nonsense
Constraint: Don't repeat strategy, call out movement
```

### Closing Loop Mode (15:30-19:00 IST)
```
Persona: Hand-off
Focus: Last work loop closed, family transition prep
Tone: One dry sentence on closing loop, then stop
Constraint: No canonical tools, resource lists, or vault items
```

### Night Mode (>19:00 IST)
```
Persona: Audit & Archive
Focus: Loops closed, home tasks, church tasks, critical tomorrow items, ideas secured
Tone: "Intel: Vaulted" / "Intel: Secured"
Structure: ✅ Done first → 🏠 Home → ⛪ Church → 🚀 Work (top 2-3) → 💡 Ideas
```

## The Horizon Guard

The briefing AI has a 2-day task horizon and a 14-day creation window:

- Tasks with reminder dates >48 hours away are hidden from the AI (prevents noise)
- Tasks created more than 14 days ago are excluded (prevents stale backlog from polluting)
- Weekend vs. weekday smart filtering based on org_tag:
  - Personal/Ashraya tasks are visible on weekends
  - Work tasks (SOLVSTRAT, CRAYON, QHORD) are de-emphasized on weekends

## The Nag Logic

If a task is both urgent AND older than 48 hours without completion, it's flagged as "stagnant" in the briefing context.

## Stale Task Detection

Tasks untouched (no update to `updated_at`) for 7+ days are surfaced in the AI context, sorted by age. The AI can optionally suggest review or re-prioritization.

## Drift Detection

The `detect_drift()` RPC checks if a project has been updated 3+ times in the last 48 hours. If so, the briefing prompt flags it as a potential bottleneck, allowing the AI to call attention to churn.

## Revenue-Critical Bolding

Tasks flagged `is_revenue_critical: true` (involving sales, pilots, payments) are rendered in **bold** within the briefing. This ensures revenue-impacting items visually stand out from the rest of the task list regardless of section placement.

The flag is set during task creation by the Pulse AI (which evaluates the task content against revenue indicators) or by explicit user assignment via the Web UI.

## Section Density Constraints

Max 3 items per section. If more exist, the AI appends: "…and X more in /library or /vault". This prevents information overload in the Telegram briefing.

## Empty Section Suppression

If a section has zero items, it's completely omitted — no "None today", no empty headers. Silence is preferred.
