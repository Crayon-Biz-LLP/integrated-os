"""
user_settings.py — per-tenant personalization loader (M2, plans/69-multi-tenant-product-plan).

The de-personalization chokepoint: every piece of per-user identity that used
to be hardcoded in code ("Danny", the Ashraya/Solvstrat/Crayon domains,
Asia/Kolkata, Rhodey's voice) now lives in the `users`/`user_settings` tables
and is read through this module. Outside the Danny-era defaults block below,
this module contains no user-specific literals — the defaults preserve the
pre-M2 behaviour when a tenant's settings are absent.

Resolution order for every field:
  1. user_settings row (per tenant) — the product form
  2. environment fallback (USER_NAME, USER_TIMEZONE) — pre-M0 single-user mode
  3. built-in defaults (Danny-era constants) — guarantees byte-for-byte
     behaviour on the existing copy DB before Danny's row is seeded

The pulse engine (cron) runs before M4's per-tenant fan-out, so it calls the
`for_user()` helpers with the user id it is serving (today: tenant #1 or the
env USER_NAME). The API layer resolves the tenant from the request context.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from core.services.db import get_supabase


# ── Defaults (Danny-era constants — preserved behaviour when unseeded) ──────

DEFAULT_USER_NAME = "Danny"
DEFAULT_TIMEZONE = "Asia/Kolkata"

# Danny's life domains — the seed values for tenant #1 (copied verbatim from
# the pre-M2 hardcoded routing rules in core/prompts/classify.py /
# core/prompts/email_classify.py / core/pulse/briefing.py). Every keyword and
# multi-word phrase in the HEAD PROJECT ROUTING clause must survive here —
# scripts/verify_m2_equivalence.py enforces this (routing keyword gate).
DEFAULT_DOMAINS: list[dict] = [
    {"name": "Solvstrat", "keywords": ["solvstrat", "tech", "client", "zoho", "api"]},
    {"name": "Qhord", "keywords": ["qhord", "os", "product", "pricing"]},
    {"name": "Crayon", "keywords": ["crayon", "corporate", "governance", "corporate governance", "business tax", "business taxes", "legal compliance"]},
    {"name": "Ashraya", "keywords": ["ashraya", "church", "ministry", "church administration", "operations", "accounts", "chennai north", "chennai central", "pastor"]},
    {"name": "Personal", "keywords": ["personal", "home", "family", "bills", "finances", "personal finances", "spiritual practices", "bible reading", "prayer", "volunteering"]},
    {"name": "Atna", "keywords": ["atna", "middleware", "platform"]},
]

# Org names treated as personal/life domains by the pulse briefing's
# work/life split (core/pulse/briefing.py) — seed value for Danny.
DEFAULT_PERSONAL_ORGS = [
    "Personal", "Ashraya", "Ashraya Chennai", "Chennai North", "Chennai Central", "Ashraya India",
]

DEFAULT_CONTEXT = "Danny (Yashwant Daniel), founder of Crayon, Chennai, India."


@dataclass
class UserSettings:
    user_id: str | None
    name: str = DEFAULT_USER_NAME
    timezone: str = DEFAULT_TIMEZONE
    domains: list[dict] = field(default_factory=lambda: list(DEFAULT_DOMAINS))
    personal_orgs: list[str] = field(default_factory=lambda: list(DEFAULT_PERSONAL_ORGS))
    voice: str | None = None
    context: str = DEFAULT_CONTEXT

    @property
    def domain_names(self) -> list[str]:
        """The domain labels, e.g. ['Solvstrat', 'Qhord', ...]."""
        return [d.get("name", "") for d in self.domains if d.get("name")]

    @property
    def domain_keywords(self) -> dict[str, list[str]]:
        """domain name (lower) -> list of lowercase routing keywords."""
        out: dict[str, list[str]] = {}
        for d in self.domains:
            name = (d.get("name") or "").strip()
            if not name:
                continue
            kws = [k.lower() for k in (d.get("keywords") or []) if str(k).strip()]
            kws.append(name.lower())
            out[name.lower()] = kws
        return out


# ── Loader (cached per process, keyed by user id) ───────────────────────────

_settings_cache: dict[str, UserSettings] = {}


def _env_name() -> str:
    return os.getenv("USER_NAME", DEFAULT_USER_NAME)


def _env_timezone() -> str:
    return os.getenv("USER_TIMEZONE", DEFAULT_TIMEZONE)


def defaults() -> UserSettings:
    """Settings for an unseeded user — env overrides on top of Danny-era defaults."""
    return UserSettings(
        user_id=None,
        name=_env_name(),
        timezone=_env_timezone(),
        domains=list(DEFAULT_DOMAINS),
        personal_orgs=list(DEFAULT_PERSONAL_ORGS),
        voice=os.getenv("RHODEY_VOICE"),
        context=os.getenv("USER_CONTEXT", DEFAULT_CONTEXT),
    )


def clear_cache(user_id: str | None = None) -> None:
    """Drop cached settings (tests / settings edits)."""
    if user_id is None:
        _settings_cache.clear()
    else:
        _settings_cache.pop(user_id, None)


def load_settings(user_id: str) -> UserSettings:
    """Read user_settings for `user_id`, merged over defaults (fail-open).

    The display name has no column on user_settings — it lives on the users
    row (users.name), so it is fetched here too; a fresh tenant therefore
    gets THEIR name in every prompt slot, not the Danny-era default.
    """
    if user_id in _settings_cache:
        return _settings_cache[user_id]

    base = defaults()
    # Name comes from the users row (fail-open: env/default name preserved).
    try:
        ures = (
            get_supabase()
            .table("users")
            .select("name")
            .eq("id", user_id)
            .limit(1)
            .maybe_single()
            .execute()
        )
        uname = (ures.data or {}).get("name") if ures.data else None
        if uname:
            base.name = str(uname).strip()
    except Exception:
        pass  # fail-open: env / default name
    try:
        res = (
            get_supabase()
            .table("user_settings")
            .select("user_id, timezone, domains, voice, context")
            .eq("user_id", user_id)
            .limit(1)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        # Pre-db/78 production / table missing: env/defaults only.
        try:
            from core.lib.audit_logger import audit_log_sync
            audit_log_sync("db", "INFO", f"user_settings load unavailable (using defaults): {type(e).__name__}")
        except Exception:
            pass
        base.user_id = user_id
        _settings_cache[user_id] = base
        return base

    row = res.data if res.data else {}
    base.user_id = user_id
    if row.get("timezone"):
        base.timezone = row["timezone"]
    if row.get("voice"):
        base.voice = row["voice"]
    if row.get("context"):
        base.context = row["context"]
    domains = row.get("domains")
    if domains:
        if isinstance(domains, str):
            try:
                domains = json.loads(domains)
            except Exception:
                domains = None
        if isinstance(domains, list) and domains:
            base.domains = [
                {
                    "name": str(d.get("name", "")).strip(),
                    "keywords": [str(k).lower() for k in (d.get("keywords") or [])],
                }
                for d in domains
                if isinstance(d, dict) and d.get("name")
            ]
            if not base.domains:
                base.domains = list(DEFAULT_DOMAINS)
    _settings_cache[user_id] = base
    return base


def resolve_user_name(user_id: str | None = None) -> str:
    """The user's display name: users.name → env → Danny-era default.

    `user_id` is required for the per-tenant name to resolve — without it
    (legacy single-user mode) the env var or the default is used.
    """
    if user_id:
        try:
            return load_settings(user_id).name or _env_name()
        except Exception:
            pass
    return _env_name()


def resolve_timezone(user_id: str | None = None) -> str:
    """The user's IANA timezone name: settings row → env → Asia/Kolkata."""
    if user_id:
        try:
            return load_settings(user_id).timezone or _env_timezone()
        except Exception:
            pass
    return _env_timezone()


def resolve_domains(user_id: str | None = None) -> list[dict]:
    """Routing domains for the classifier/pulse: settings row → defaults."""
    if user_id:
        try:
            return load_settings(user_id).domains or list(DEFAULT_DOMAINS)
        except Exception:
            pass
    return list(DEFAULT_DOMAINS)


def resolve_personal_orgs(user_id: str | None = None) -> list[str]:
    """Personal/life org names for the pulse work-life split."""
    if user_id:
        try:
            return load_settings(user_id).personal_orgs or list(DEFAULT_PERSONAL_ORGS)
        except Exception:
            pass
    return list(DEFAULT_PERSONAL_ORGS)


def resolve_context(user_id: str | None = None) -> str:
    """One-line 'who they are' for prompt slots."""
    if user_id:
        try:
            return load_settings(user_id).context or DEFAULT_CONTEXT
        except Exception:
            pass
    return os.getenv("USER_CONTEXT", DEFAULT_CONTEXT)


def routing_rules_text(user_id: str | None = None) -> str:
    """Render the domain routing rules for the classify prompt.

    Produces one line per domain: keywords → entity, mirroring the pre-M2
    hardcoded PROJECT ROUTING rule so Danny's routing is byte-identical once
    his domains are seeded.
    """
    domains = resolve_domains(user_id)
    lines = []
    for d in domains:
        name = (d.get("name") or "").strip()
        if not name:
            continue
        kws = [str(k) for k in (d.get("keywords") or []) if str(k).strip()]
        if kws:
            lines.append(f"- Keywords {', '.join(kws)} → {name}")
    if not lines:
        return ""
    return "PROJECT ROUTING (by user's life domains):\n" + "\n".join(lines)


# Kept for callers that only need the tenant context's user id without
# importing the tenant machinery directly.
def current_user_id() -> str | None:
    """User id from the request tenant context, if any (M1)."""
    try:
        from core.services.db import get_tenant
        return get_tenant()
    except Exception:
        return None
