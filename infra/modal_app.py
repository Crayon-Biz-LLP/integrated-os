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
