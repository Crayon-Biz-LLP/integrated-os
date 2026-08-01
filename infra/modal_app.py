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
_BUILD_VERSION = "v6"

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
# timeout=300: 5 min for web requests (was 60s on Vercel).


@app.function(
    secrets=secrets,
    min_containers=1,
    scaledown_window=300,
    timeout=300,
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

    payload: {"fake_update": dict, "session_id": str | None}

    Delegates to api.index._run_web_message_pipeline — the exact same code
    path the inline fallback uses, so behavior is identical everywhere.
    """
    import asyncio
    from api.index import _run_web_message_pipeline

    fake_update = payload.get("fake_update")
    session_id = payload.get("session_id")
    if not fake_update:
        print("[process_message_background] Missing fake_update — aborting")
        return
    asyncio.run(_run_web_message_pipeline(fake_update, session_id))
