# Phase 2B — Per-tenant persona on the remaining surfaces

> Status: **DELIVERED (Steps 0–5)** — one commit (Steps 0–5). Built against
> the M18c architectural rule ("persona is Layer-3 knowledge") and the
> 6-layer model. Hardened by construction: contract-first, single homes,
> per-step proof. Deploy rhythm: server-only commit (Steps 0–2, no APK) + a
> subsequent APK rebuild for the Step 4 client lines.

## 0. Architectural contract (inherited from M18c + 6-layer model)

| # | Rule | Enforced by |
|---|---|---|
| R1 | The persona card is **L3 knowledge**. It is read in exactly one place: `core/services/persona.py` (service layer, tenant-scoped). | AST gate (`test_persona_l3_context.py`) |
| R2 | **Message composition has one home**: `core/services/message_voice.py` (service layer). Pulse, API, and push all consume it — no inline composition in any layer. | AST gate + code review |
| R3 | Every persona-derived **output** string passes `persona_guard_text` with a neutral fallback. | `test_guard_coverage` |
| R4 | **Client rendering is a closed enum.** The app receives a `voice_style` token (`direct` \| `calm` \| `warm`) — never prose, never the card, never curated people names. | Shape test on summary |
| R5 | **Fail-closed**: no card → every surface byte-identical to today. | Matrix (a) per step |
| R6 | No DB migration. Card stays in `core_config`. New surfaces are transport + composition only. | — |

## 1. Layer map of the change

| Concern | Layer | Home |
|---|---|---|
| Card read | L3 service | `core/services/persona.py` (existing) |
| Surface summary | L3 service | `persona_surface_summary()` — same file |
| Message templates | L4→ service | **NEW** `core/services/message_voice.py` |
| API transport | L6/API | `GET /api/persona` + `/api/home-feed` persona block |
| Client rendering | L5 | Flutter — closed enum switch only |

## 2. Steps — each with its Definition of Done

### Step 0 — Service layer: summary + composer (0.5–1d)
- `persona_surface_summary()` → safe display dict:
  `{display_name, voice_style (closed enum), signoffs (≤2)}`.
  **No curated names, no never-topics, no card JSON.** Fail-closed `None`.
  (`greeting_fallback` intentionally omitted — greetings are server-composed;
  the app needs only the display name to render its own fallbacks.)
- **NEW `core/services/message_voice.py`** — all proactive-copy templates:
  - focal-action confirmations (done / snoozed / correct) — persona-gated
    (new personalization; neutral for everyone today)
  - decision-pulse push title/body (ported from `decision_pulse.py:285-299`)
    — the name opener is TENANT IDENTITY, keyed off a `name` data param, so
    card-less tenants stay byte-identical (R5); only the never-guard applies
  - Every template's final output passes `persona_guard_text` with the
    neutral form as fallback.
- **DoD:** `test_guard_coverage` — every template's persona form is guarded
  (a planted never-topic collapses to neutral); no-card path returns the
  neutral form **byte-identical**; `py_compile` + `ruff` green.

### Step 1 — API transport (0.5–1d, no APK)
- `GET /api/persona` — `require_api_auth`, tenant-scoped, returns the summary.
- `/api/home-feed` gains a `persona` block in the **same round-trip** (thread-
  offloaded; tenant contextvar propagates into the worker thread).
- **DoD (matrix):** (a) no card → `persona: null` (home-feed gains a null
  `persona` key; all existing keys unchanged — additive and safe for current
  consumers); (b) tenant A summary ≠ tenant B (isolation); (d) negative AST
  test — a planted `resolve_persona` import in `api/` is **caught**, and the
  gate now also scans `core/services/message_voice.py` so R2 (the composer
  never reads the card) is enforced, not just documented.

### Step 2 — Focal-action + decision-pulse copy on `message_voice` (1d, no APK)
- `api/index.py` focal-action responses call `message_voice.compose_*`.
- **`decision_pulse.py` refactored ONTO `message_voice`** — inline composition
  (`decision_pulse.py:285-299`) deleted in the same commit. Drift removed, not added.
- **DoD (matrix):** (a) no-card messages byte-identical to today's strings;
  (c) guard coverage; decision-pulse push text unchanged for no-card tenants.

### Step 3 — Snooze messaging (1d; server half no APK, client half needs APK)
- Server: ladder logic **untouched** (already correct — absolute UTC expiry,
  verified timezone-safe). Confirmation copy via `message_voice`.
- Client: the 3rd-snooze warning + feedback prompt switches on the
  `voice_style` token — folded into Step 4's APK.
- **DoD (matrix):** (a)(c). Timezone note: ladder stays absolute-UTC; tenant
  timezone matters only if 2B ever adds day-boundary snooze.

### Step 4 — App home (1–1.5d, APK)
- Greeting / "All clear" / tiny-brief fallback switch on the `voice_style`
  enum only; server-composed strings win wherever they exist.
- Arrow-callout home tour (from the onboarding discussion) lands here.
- **DoD:** widget tests for each enum variant; the app persists only the token.

### Step 5 — Gates, boundary, docs (0.5d)
- **AST gate extended to `api/` AND `frontend/src`** — negative test proves
  both are caught.
- **Scope boundary** (below) documented; any future surface lands under R1–R5.
- Session note `72` updated with layer-checklist entries: *API transport*,
  *message composition*, *client rendering (closed enum)*.
- **DoD:** full battery green, M9.2/M9.3 gates, residue scan (live + offline).

## 3. Test matrix — applied to *every* step (prove behavior, not lint)

| Test | What it proves |
|---|---|
| (a) fail-closed byte-identical | No card → today's exact strings. |
| (b) per-tenant isolation | Danny's persona never appears in Johan's payloads. |
| (c) guard coverage | Every new message template passes `persona_guard_text`. |
| (d) negative AST gate | A planted direct card read in `api/` or `frontend/` fails CI. |

## 4. Scope boundary

**In scope:** persona transport to the app (summary), persona-toned message
composition (focal-action, push), client rendering via closed enum.

**Out of scope (unchanged):** L1 ingestion, Telegram surface, dashboard admin,
briefing/sentinel/reply generation (already L3), classify sign-offs (L3
accessor), snooze ladder logic, all DB schema. **Any future surface lands
under R1–R5 before it ships.**

## 5. Deploy rhythm

```
Commit 1: Steps 0–2   → push → Modal deploy → NO APK   (server-only)
Commit 2: Steps 3c+4  → APK rebuild                    (client copy + home polish)
Commit 3: Step 5      → gates + docs → CI green        (the "write in your brain" close)
```

## 6. Decisions (baked in)

1. **`message_voice.py` is the composer home** — consolidates decision_pulse's
   existing inline drift in the same commit that adds focal-action copy.
2. **Curated names never ship to the device** — the tiny brief is server-
   composed; the phone gets only `voice_style` + display name. Minimal data at
   rest = smaller privacy surface.
