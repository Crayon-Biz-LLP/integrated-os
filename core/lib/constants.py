import os


class EmailStatus:
    NEW         = "new"
    PROCESSING  = "processing"
    PROCESSED   = "processed"
    NEEDS_REPLY = "needs_reply"
    SNOOZED     = "snoozed"
    ERROR       = "error"
    IGNORED     = "ignored"


# Sender names to exclude from context queries — these are Rhodey's own responses
# that should never be fed back as current context (causes hallucination loops)
BOT_SENDERS = {'rhodey_bot', 'rhodey', 'assistant', 'bot'}

# Tenant #1 (Danny) GitHub Actions dispatch target — the SINGLE source of
# truth. Used as the legacy default in resolve_github_config() AND written
# into core_config by scripts/seed_tenant1_m6_config.py (keep in sync via
# that seed).
DEFAULT_GITHUB_OWNER = "Crayon-Biz-LLP"
DEFAULT_GITHUB_REPO = "integrated-os"


def resolve_github_config() -> tuple[str, str]:
    """GitHub Actions dispatch target (owner, repo) — per-tenant config.

    Resolution: core_config ('github_owner' / 'github_repo') → env
    (GITHUB_OWNER / GITHUB_REPO) → Danny-era defaults (Crayon-Biz-LLP /
    integrated-os). Reads the tenant-scoped core_config when a tenant
    context is active; env fallback covers legacy/deployment config.
    """
    owner = None
    repo = None
    try:
        from core.services.db import tenant_aware_client
        supabase = tenant_aware_client()
        res = supabase.table("core_config").select("key, content").in_("key", ["github_owner", "github_repo"]).execute()
        for r in (res.data or []):
            if r.get("key") == "github_owner" and r.get("content"):
                owner = str(r["content"]).strip()
            elif r.get("key") == "github_repo" and r.get("content"):
                repo = str(r["content"]).strip()
    except Exception:
        pass
    owner = owner or os.getenv("GITHUB_OWNER") or DEFAULT_GITHUB_OWNER
    repo = repo or os.getenv("GITHUB_REPO") or DEFAULT_GITHUB_REPO
    return owner, repo

