# 75. Persona Layer (M15 / M18 / Phase 2B)

> Verified against code 2026-08-15. Rhodey speaks *per tenant* — the persona is
> one grounded card per user, never shared, fail-closed to neutral.

## The model

- **One grounded card per tenant**, stored in `core_config` under key
  `persona` (`core/services/persona.py`). The card is synthesized by
  `core/skills/persona_synthesis.py` from the tenant's own history — not a
  prompt-role costume.
- **`persona_prev`** — the previous card is kept as the rollback source
  (M18 versioning): a bad synthesis can be reverted, never silently shipped.
- **Fail-closed semantics**: if the card is missing or invalid, Rhodey falls
  back to neutral prose. **A persona can never be another tenant's** — the
  read path is tenant-scoped like everything else (doc 70).
- **Grounding verification** — every prose claim is checked by
  `core/services/persona_verifier.py` (G1–G4 grounding rules) before it can
  ship; unverifiable claims are dropped.

## Where it surfaces

- **Vocabulary picker (M15)** — the user picks vocabulary/domains
  (`user_settings.domains`), which constrains the persona's word choices.
- **Layer-3 knowledge (M18)** — the persona is wired as a ContextProvider
  accessor, so briefings, queries, and messages all *know* the persona without
  special-casing (Phase 2B surfaces).
- **Voice** — `user_settings.voice` pairs with the persona for the spoken
  surface (`core/services/message_voice.py`, `rhodey_voice.py`).
- **Onboarding** — persona setup is part of sign-up
  (`user_settings.onboarding_state`, `core/services/onboarding.py`).

## Learning-loop connection

Persona corrections and vocabulary picks land in the decision/telemetry layer
(doc 71), so the persona adapts from every explicit user choice instead of
resetting to defaults.
