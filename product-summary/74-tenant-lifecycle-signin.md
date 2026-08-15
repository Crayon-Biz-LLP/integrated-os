# 74. Tenant Lifecycle & Sign-In

> Verified against code 2026-08-15. The API-key paste is dead — sign-in is
> Google / email-OTP, tenants are capped and metered, and pending questions
> escalate instead of rotting.

## Sign-in (replaced the API-key paste, M11)

- **Email-OTP** (`core/services/auth.py`): `send_otp(email)` creates a
  `login_otps` row (`code_hash`, `attempts`, `expires_at`, `consumed_at`,
  peppered with `OTP_PEPPER`) and emails the code via
  `core/services/otp_email.py`. **`verify_otp(email, code)` creates the tenant
  on first successful use** — self-serve sign-up, no admin step. `login_otps`
  is auth state, not tenant data (no `owner_id`).
- **Google sign-in** — `users.google_connected` + `user_oauth_tokens`
  (provider `google`) for calendar/tasks/gmail scopes.
- **Onboarding state machine** — `user_settings.onboarding_state`
  (`core/services/onboarding.py`) drives the app's sign-up flow.
- **Provisioning cap** — `MAX_TENANTS` env (0 = unlimited) fails self-serve
  sign-up with a clear "cap reached" error rather than silently widening the
  fleet.

## Spend caps & metering (M6)

- Every LLM call is metered into **`llm_spend`** (`owner_id`, `provider`,
  `model`, `workload`, `outcome`, tokens, `est_cost_usd`).
- Per-tenant caps: `users.monthly_credit_usd` + `credit_cycle_day` — the cap
  enforcement reads the metered spend against the monthly credit.
- `model_registry` keeps call-level telemetry (latency, tokens, success) for
  degradation detection.

## Channel binding

`users.telegram_chat_id` binds the tenant to its chat; the webhook resolves the
tenant per incoming message (`resolve_channel_tenant()`, `core/services/db.py`).
The test sandbox uses a dedicated `TEST_CHAT_IDS` allow-list (default-off) —
see doc 72.

## The snooze/escalation ladder (`core/services/awaiting_reply.py`)

Questions Rhodey asked but that haven't been answered don't silently rot:

- `mark_chat_awaiting_reply(...)` — record an open ask with channel + expiry.
- `find_open_ask(...)` — check before piling on another question.
- `resolve_awaiting_reply(...)` — clear on answer; `auto_resolve_on_outgoing`
  resolves when the user's own outgoing message answers it.
- `expire_stale_asks(...)` — sweep expired asks into the escalation path
  (nudge / surface in the next pulse).

Table: `awaiting_reply` (`asked_at`, `expires_at`, `replied_at`, `status`,
`linked_message_id`, `channel`, `chat_id`).
