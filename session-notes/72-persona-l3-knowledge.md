# 72 — Persona Is Layer-3 Knowledge (M18c Architectural Rule)

**Status:** Enforced by `tests/unit/test_persona_l3_context.py` (AST gate) +
`persona_verifier.py` count gates. Read this before touching ANY prompt,
pulse, or persona code.

---

## The rule (non-negotiable)

**The persona card is Layer 3 (Intelligence) knowledge** — the same layer as
memories, people, orgs, and graph edges. It is NOT a Layer 4 (Presentation)
string.

1. **Generators never read the card directly.** No `persona_voice_block()` /
   `resolve_persona()` import in `core/pulse/*`, `core/prompts/*`,
   `core/webhook/*`, or `core/skills/*` — EXCEPT the allowlisted L3 readers:
   `core/services/persona.py`, `core/services/persona_verifier.py`,
   `core/skills/persona_synthesis.py`, `core/pulse/context.py`.
2. **All persona knowledge enters generation through the ContextProvider**
   (`core/pulse/context.py`):
   - `hydrate_persona_context()` — async; the who/voice/never/life block for
     LLM prompts (briefing, sentinel, reply generation).
   - `persona_signoffs_context()` — sync; the card's sign-offs for prompt
     builders that cannot await (classify's receipt).
3. **Output guarding stays at L4.** `persona_guard_text()` (never-guard on
   push banners, previews) is presentation post-processing — allowed and
   unchanged. The LAYER line is between *input knowledge* (must flow through
   L3) and *output filtering* (L4 is fine).
4. **Prompt builders are presentation-only.** If a builder needs persona
   input, the async caller fetches it via the ContextProvider and passes it
   as a parameter (see `build_interrogate_brain_prompt(persona_context=...)`
   in `core/prompts/query.py`).

The AST gate in `test_persona_l3_context.py::test_l3_accessor_is_only_card_read_path_for_generators`
scans the four runtime trees with the allowlist above — a NEW generator file
cannot bypass it.

## Why this rule exists (the miss this fixes)

M18 shipped the persona as a Layer-4 string-append: `persona_voice_block()`
was imported directly at each prompt site. That worked (safe, fail-closed,
gates green) but was architecturally wrong:

- The curated life circle never entered the L3 context assembly pipeline
  alongside memories/tasks/people, so generators could not REASON about the
  user's world the way they reason about other knowledge — it was a voice
  line, not context.
- Each generator had its own inline read — drift waiting to happen.

The user's call: this is the same class as the tenant-scoping pass — build it
on the layered architecture, not as a quick fix.

## The second bug this fixes: the write/read contract

**Danny's applied card was dormant for a different reason.** The synthesis
extracted up to 12 people, the verifier had NO count gate, and the read path
(`validate_card_shape`) requires ≤ 10 — so an 11-person card passed
verification, was written, and was then silently rejected at read time:
`resolve_persona()` → None, persona layer off despite the apply.

**The contract is now enforced at write time, mirrored everywhere:**

| Field | Cap (read path = verifier = synthesis) |
|---|---|
| `people` | ≤ 10 |
| `domains` | ≤ 8 |
| `signoffs` | 2–4 |
| `claims` | ≤ 20 |
| `life_snapshot` | ≤ 12 |

- `persona_verifier.py`: count gates added (rejects over-contract cards).
- `persona_synthesis.py`: extraction capped at the contract + write-time
  truncation backstop.
- `test_contract_verifier_read_path_agree` pins that a verifier-passing card
  always passes `validate_card_shape` — the two can never drift again.

**Ordering note (so nobody calls the verifier gates "dead code"):** the
synthesis CLAMPS the card to the caps BEFORE `verify_persona_card` runs, so
in the synthesis path the count gates can never fire — they are the
BACKSTOP for future non-synthesis callers, not the primary enforcement.
Clamp-then-verify is the sound order: clamping only reduces sets, so it can
never newly violate a count gate, and it keeps the verifier rejections to
cards that are wrong beyond repair (unknown entities, fabricated claims).

**Operational note:** a card already stored that fails the read path looks
like "persona applied but nothing changed." If `resolve_persona()` returns
None while a `core_config.persona` row exists, re-run the synthesis
(`python3 -m core.skills.persona_synthesis --user <name>`) — it rewrites the
card within the contract. Check with:
`python3 -m core.skills.persona_synthesis --user <name> --dry-run`.

## The layer checklist (before adding any new generator)

1. Does it generate language? → consume knowledge via `ContextProvider`,
   never import the card.
2. Does it post-process output? → `persona_guard_text` is fine (L4).
3. Does it write a card or facts? → verifier + synthesis contract applies.
4. Run the AST gate + `test_contract_verifier_read_path_agree` before commit.

---

# Phase 2B — Per-tenant persona on the remaining surfaces (plan `plans/72`)

**Status:** DELIVERED (Steps 0–5). Read this before touching message copy,
the API surface, or the Flutter voice lines. Built on the M18c rule above;
all five rules (R1–R5) extend unchanged.

## New layer-checklist entries (Phase 2B)

1. **API transport (R4)** — the app receives the persona ONLY as a surface
   summary: `GET /api/persona` and the `/api/home-feed` `persona` key, both
   built by `core/services/persona.py::persona_surface_summary()` — a closed
   enum (`display_name`, `voice_style` ∈ direct|calm|warm, `signoffs` ≤ 2).
   Never the raw card, curated people, or never-topics. Fail-closed `null`.
   The AST gate now scans `api/`; the frontend gate scans `frontend/src`
   (`test_frontend_never_reads_persona_card`).
2. **Message composition (R2)** — all proactive copy composes in ONE home:
   `core/services/message_voice.py` (done/snoozed/corrected confirmations +
   decision-pulse push title/body). Every template = `(neutral, persona)`
   pair, final output through `persona_guard_text` with the neutral form as
   fallback. `test_r2_composition_lives_in_message_voice` asserts the pulse
   and API layers import the composer and never drag the guard back inline.
   NOTE: the push opener is TENANT IDENTITY (a `name` data param), not
   persona content — card-less tenants stay byte-identical (R5).
3. **Client rendering (R4)** — the Flutter app switches Rhodey's OWN static
   lines on the `voice_style` closed enum only (`RhodeyVoice.allClear*`;
   the snooze-gate feedback hint). Server-composed strings (briefing,
   focal confirmations) always take precedence; unknown style => standard
   voice byte-identical pre-2B. The home tour overlay (`home_tour_overlay`)
   already covers the tiny brief + focal card + time-of-day callouts.

## The dormant-card lesson applies to every new persona surface

If a persona surface "does nothing" after a card is applied, check the
read-path shape validator FIRST (people ≤ 10 / domains ≤ 8 / signoffs 2–4 /
claims ≤ 20 / life_snapshot ≤ 12) — the write/read contract is enforced at
write time, so a stored card that fails validation means it predates the
contract and must be re-synthesized (`--user <name>`).
