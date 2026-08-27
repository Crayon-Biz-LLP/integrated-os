"""
Modal deployment entry point for Rhodey OS.

This file replaces Vercel as the runtime for the FastAPI backend.
All scheduled background jobs remain on GitHub Actions or cron-job.org.

Deploy:
    modal deploy infra/modal_app.py

Develop:
    modal serve infra/modal_app.py     (live-reloads on file changes)
"""

import modal

# ── Image Definition ──────────────────────────────────────────────
# Installs all project dependencies into a Debian slim base.
# add_local_dir includes local source dirs in the container image
# (api/ and core/ aren't in infra/, so we need to add them explicitly).
# Note: during modal serve, these are copied at build time, not live-reloaded.
# Build cache version — increment to force a fresh image build
_BUILD_VERSION = "v7"

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements("requirements.txt")
    .apt_install("ffmpeg")
    .env({"BUILD_VERSION": _BUILD_VERSION})
    .add_local_dir("./api", remote_path="/root/api")
    .add_local_dir("./core", remote_path="/root/core")
)

# ── Modal App ─────────────────────────────────────────────────────
app = modal.App("rhodey-os", image=image)
secrets = [modal.Secret.from_name("rhodey-os")]


# ── Web Endpoint: All API Routes ──────────────────────────────────
# min_containers=1: keeps one container always warm — zero cold starts.
# scaledown_window=300: if no requests for 5 min, scale to 0.
# timeout=900: 15 min for web requests (was 60s on Vercel, then 300s on
# Modal). Raised so the /api/pulse-cron inline fallback (and any heavy
# web-triggered path) has headroom — the primary briefing path is the
# per-tenant brief_tenant worker below, which also runs at 900s.


@app.function(
    secrets=secrets,
    min_containers=1,
    scaledown_window=300,
    timeout=900,
)
@modal.concurrent(max_inputs=10)
@modal.asgi_app()
def web_endpoint():
    """All API routes served from api/index.py.

    Returns the original FastAPI app directly — preserves all routes,
    middleware, exception handlers, and startup events. No fragile
    route-copying needed.

    Serves all endpoints including:
      - /api/webhook (Telegram)
      - /api/sentinel (called by cron-job.org every 5 min)
      - /api/decision-pulse (called by cron-job.org every 30 min)
      - /api/roundup (called by cron-job.org 2x daily)
      - /api/health
      - All dashboard/API proxy routes
    """
    from api.index import app as fastapi_app
    return fastapi_app


# ── Background Message Worker (P3 fast-ack) ────────────────────
# /api/send-message returns instantly and spawns THIS function on a dedicated
# container. Because it's a separate Modal function (not an asyncio task in
# the web container), it survives the web request's return — Modal keeps the
# worker alive until it completes. The full pipeline (classify → entity
# extraction → route → LLM reply → push) runs here, then the reply reaches
# the app via the FCM push fired inside send_telegram + the backup poll.
@app.function(
    secrets=secrets,
    timeout=300,
    # Keep one worker warm so the reply path has NO cold start — the web
    # endpoint already runs min_containers=1; the background worker must
    # too, or the first send after an idle window pays a 5-15s container
    # boot before the LLM even starts. Same scaledown_window as web.
    min_containers=1,
    scaledown_window=300,
)
def process_message_background(payload: dict):
    """Background worker for /api/send-message fast-ack.

    payload: {"fake_update": dict, "session_id": str | None, "uid": str | None}

    Delegates to api.index._run_web_message_pipeline — the exact same code
    path the inline fallback uses, so behavior is identical everywhere.

    Tenant re-scope (REQUIRED): the worker runs in a SEPARATE Modal
    container, so the tenant contextvar set by require_api_auth() in the web
    request does NOT propagate here. Without an explicit tenant_scope(uid),
    webhook_tenant_scope() finds no active tenant and falls back to
    resolve_channel_tenant() = the first active user (tenant #1) — meaning
    every tenant #2 message would silently run under tenant #1's world
    (real cross-tenant bug, Aug 8). The web route passes the authenticated
    uid explicitly; we re-scope here.
    """
    import asyncio
    from api.index import _run_web_message_pipeline

    fake_update = payload.get("fake_update")
    session_id = payload.get("session_id")
    uid = payload.get("uid")
    if not fake_update:
        print("[process_message_background] Missing fake_update — aborting")
        return
    if uid:
        from core.services.db import tenant_scope
        with tenant_scope(uid):
            asyncio.run(_run_web_message_pipeline(fake_update, session_id))
    else:
        # Legacy shared-key / pre-db/78: no tenant context existed in the web
        # route either, so the channel-tenant fallback is the original
        # behavior — preserve it exactly.
        asyncio.run(_run_web_message_pipeline(fake_update, session_id))


# ── Per-Tenant Briefing Worker (Option B) ───────────────────────────
# The 30-min heartbeat (/api/pulse-cron, fired by cron-job.org) no longer
# runs every tenant's briefing sequentially inside the web request — that
# sequential fan-out overran the 300s web timeout and killed every tenant
# after the first (real outage: Johan/Sunjula/Test got no briefings for
# 28h while Danny's stayed fresh). Instead the web endpoint spawns ONE of
# these per DUE tenant, in parallel, each with its own 900s timeout and its
# own tenant scope — a slow tenant can no longer starve or kill the others,
# and the fan-out scales to any number of tenants (each gets a container).
# Same pattern as process_message_background: separate container, explicit
# tenant_scope(uid) re-applied because the contextvar does NOT propagate
# across containers.
@app.function(
    secrets=secrets,
    timeout=900,
)
def brief_tenant(uid: str, auth_secret: str | None = None, trigger: str = "cron"):
    """Run ONE tenant's briefing in a dedicated container.

    Delegates to core.pulse.briefing.process_pulse_for_tenant — the exact
    per-tenant unit the inline loop uses — so behavior is identical whether
    a briefing runs in-request or here. The per-tenant concurrency lock
    (acquired inside _process_pulse_impl) prevents duplicate runs of the
    same tenant even if two heartbeats overlap.
    """
    import asyncio
    from core.pulse.briefing import process_pulse_for_tenant

    result = asyncio.run(
        process_pulse_for_tenant(uid, auth_secret=auth_secret, trigger=trigger)
    )
    print(f"[brief-tenant:{uid[:8]}] {result}", flush=True)
    return result


# ── Beeper Bridge (Phase B1): sync the Matrix stream every 60s ───────
# PAUSED (Aug 13): the scheduled tick is removed. The VPS Desktop bridge
# (core/skills/beeper_desktop.py, cron every 5 min on the always-on Oracle
# box) is now the primary capture path — it reads the Desktop API token from
@app.function(
    secrets=secrets,
    timeout=900,
    min_containers=0
)
def process_suggestion_confirm_background(payload: dict):
    """Background worker for /api/suggestions/confirm heavy lifting."""
    owner_id = payload.get("owner_id")
    if not owner_id:
        print("[process_suggestion_confirm_background] Missing owner_id")
        return
        
    from core.services.db import tenant_scope
    with tenant_scope(owner_id):
        from api.index import _run_suggestion_confirm_background
        import asyncio
        asyncio.run(_run_suggestion_confirm_background(payload))

# the VPS .env, not the Modal secret, and it works with the Mac off. The
# legacy Matrix token here is dead, and two pollers would double the LLM
# cost. The function stays defined so it can be invoked manually if ever
# needed, but it no longer auto-fires. To re-enable, restore:
#     schedule=modal.Period(seconds=60),
@app.function(
    secrets=secrets,
    # PAUSED: schedule=modal.Period(seconds=60),
    # Headroom for the FIRST tick: an initial /sync over 3,391 rooms is a
    # large payload; subsequent incremental syncs are tiny. 300s covers the
    # cold-start full sync comfortably.
    timeout=300,
)
def beeper_bridge_sync():
    """Scheduled bridge tick: fan out the Beeper Matrix sync per tenant."""
    import asyncio
    from core.skills.beeper_ingest import run_beeper_sync
    result = asyncio.run(run_beeper_sync())
    print(f"[beeper-bridge] {result}", flush=True)
    return result
