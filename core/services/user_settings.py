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
import re
from dataclasses import dataclass, field

from core.services.db import get_supabase, tenant_aware_client


# ── Defaults (Danny-era constants — preserved behaviour when unseeded) ──────

DEFAULT_USER_NAME = "Danny"
DEFAULT_TIMEZONE = "Asia/Kolkata"

# Danny's life domains — the seed values for tenant #1 (copied verbatim from
# the pre-M2 hardcoded routing rules in core/prompts/classify.py /
# core/prompts/email_classify.py / core/pulse/briefing.py). Every keyword and
# multi-word phrase in the HEAD PROJECT ROUTING clause must survive here —
# scripts/verify_m2_equivalence.py enforces this (routing keyword gate).
# Each entry carries an is_personal flag for the work/life split.
DEFAULT_USER_ORGS: list[dict] = [
    {"name": "Solvstrat", "keywords": ["solvstrat", "tech", "client", "zoho", "api"], "is_personal": False},
    {"name": "Qhord", "keywords": ["qhord", "os", "product", "pricing"], "is_personal": False},
    {"name": "Crayon", "keywords": ["crayon", "corporate", "governance", "corporate governance", "business tax", "business taxes", "legal compliance"], "is_personal": False},
    {"name": "Ashraya", "keywords": ["ashraya", "church", "ministry", "church administration", "operations", "accounts", "chennai north", "chennai central", "pastor"], "is_personal": True},
    {"name": "Personal", "keywords": ["personal", "home", "family", "bills", "finances", "personal finances", "spiritual practices", "bible reading", "prayer", "volunteering"], "is_personal": True},
    {"name": "Atna", "keywords": ["atna", "middleware", "platform"], "is_personal": False},
]

# Backward-compatible aliases for any remaining un-migrated callers.
DEFAULT_DOMAINS = DEFAULT_USER_ORGS
DEFAULT_PERSONAL_ORGS = ["Personal", "Ashraya", "Ashraya Chennai", "Chennai North", "Chennai Central", "Ashraya India"]

DEFAULT_CONTEXT = "Danny (Yashwant Daniel), founder of Crayon, Chennai, India."

# Night sign-off options in the classify receipt. Danny's seeded core_config
# row keeps his personal line; the unscoped/env default reproduces his exact
# pre-M17 behaviour for legacy CLI paths. A tenant-scoped call NEVER inherits
# this — it resolves from their own core_config row (or the neutral list).
DEFAULT_NIGHT_SIGNOFFS = '"Now go be a dad." / "Rest well." / "Locked in for the night."'
NEUTRAL_NIGHT_SIGNOFFS = '"Rest well." / "Locked in for the night."'

# Tenant #1's Vault URL — legacy unscoped fallback + seed value (M17). The
# /vault command resolves per-tenant from core_config; a tenant without a row
# gets "not configured", never this URL.
DEFAULT_VAULT_URL = "https://danny-integrated-os.streamlit.app"
NEUTRAL_VAULT_URL = ""


@dataclass
class UserSettings:
    user_id: str | None
    name: str = DEFAULT_USER_NAME
    timezone: str = DEFAULT_TIMEZONE
    user_orgs: list[dict] = field(default_factory=lambda: list(DEFAULT_USER_ORGS))
    voice: str | None = None
    context: str = DEFAULT_CONTEXT

    @property
    def user_org_names(self) -> list[str]:
        """The org labels, e.g. ['Solvstrat', 'Qhord', ...]."""
        return [d.get("name", "") for d in self.user_orgs if d.get("name")]

    # Backward-compatible aliases for callers not yet migrated.
    @property
    def domains(self) -> list[dict]:
        return self.user_orgs

    @property
    def domain_names(self) -> list[str]:
        return self.user_org_names

    @property
    def personal_orgs(self) -> list[str]:
        return [d.get("name", "") for d in self.user_orgs if d.get("is_personal")]


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
        user_orgs=list(DEFAULT_USER_ORGS),
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
        # Select * to be resilient across schema transitions:
        # - Before migration 106: user_orgs column doesn't exist yet
        # - After migration 107: domains/personal_orgs columns are dropped
        res = (
            get_supabase()
            .table("user_settings")
            .select("*")
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
    # M17: an EXISTING row is authoritative — a null/empty field means "not
    # set by this tenant" and resolves to neutral ("" / []), never tenant
    # #1's values. Danny's row carries his own context/user_orgs,
    # so his rendering is unchanged (proven by the M6/M9 gates + goldens).
    if "voice" in row:
        base.voice = row.get("voice") or ""
    if "context" in row:
        base.context = (row.get("context") or "").strip()
    # Read user_orgs (preferred) — fall back to legacy domains+personal_orgs
    if "user_orgs" in row and row["user_orgs"] is not None:
        parsed = _parse_user_orgs(row["user_orgs"])
        if parsed is not None:
            base.user_orgs = parsed
    elif "domains" in row:
        # Legacy path: migrate domains+personal_orgs into user_orgs shape
        parsed_domains = _parse_domains(row["domains"])
        personal_orgs_list = []
        if "personal_orgs" in row:
            _po = row.get("personal_orgs")
            if isinstance(_po, str):
                try:
                    _po = json.loads(_po)
                except Exception:
                    _po = None
            personal_orgs_list = [
                str(x).strip() for x in (_po or []) if str(x).strip()
            ]
        if parsed_domains is not None:
            base.user_orgs = [
                {
                    "name": d.get("name", ""),
                    "keywords": d.get("keywords", []),
                    "is_personal": d.get("name", "") in personal_orgs_list,
                }
                for d in parsed_domains
                if d.get("name")
            ]
    _settings_cache[user_id] = base
    return base


def _parse_user_orgs(raw) -> list[dict] | None:
    """Parse stored user_orgs into dict form with is_personal flag.

    Returns None when the value is a legacy string-array (tenant #1's stored
    shape) — callers then keep the DEFAULT_USER_ORGS fallback. Returns [] for
    a present-but-empty value so a tenant with no orgs gets neutral.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    user_orgs = raw
    if isinstance(user_orgs, str):
        try:
            user_orgs = json.loads(user_orgs)
        except Exception:
            return []
    if not isinstance(user_orgs, list):
        return []
    if all(isinstance(d, str) for d in user_orgs):
        # Legacy tenant-#1 shape (plain name strings) → keep default routing.
        return None
    parsed = [
        {
            "name": str(d.get("name", "")).strip(),
            "keywords": [str(k).lower() for k in (d.get("keywords") or [])],
            "is_personal": bool(d.get("is_personal", False)),
        }
        for d in user_orgs
        if isinstance(d, dict) and d.get("name")
    ]
    return parsed


def _parse_domains(raw) -> list[dict] | None:
    """Legacy parser — delegates to _parse_user_orgs, stripping is_personal."""
    parsed = _parse_user_orgs(raw)
    if parsed is None:
        return None
    return [{"name": d["name"], "keywords": d["keywords"]} for d in parsed]


def _effective_user_id(user_id: str | None) -> str | None:
    """Explicit user id, else the ACTIVE tenant context, else None.

    This is the de-personalization safety net: every resolve_* helper here
    was being called without a user id from tenant-scoped code (pulse,
    webhook, prompts) and silently fell back to the env/Danny-era default —
    so tenant #2's briefing voice line, prompts, and domains all rendered
    "Danny"/Danny's world (cross-tenant privacy leak, Aug 9). Resolving the
    tenant from the ambient contextvar means a tenant-scoped caller NEVER
    inherits tenant #1's identity; only truly unscoped legacy code (CLI,
    pre-db/78) still gets the env/default path.
    """
    if user_id:
        return user_id
    return current_user_id()


def resolve_user_name(user_id: str | None = None) -> str:
    """The user's display name: users.name → env → Danny-era default.

    `user_id` may be omitted — the ACTIVE tenant context is then used, so a
    tenant-scoped caller always resolves THEIR name (never tenant #1's). The
    env var / Danny-era default is only the legacy unscoped fallback.

    Privacy guarantee: once a tenant identity is resolved, we NEVER fall
    through to tenant #1's literal ("Danny") — a tenant with no name on file
    returns "" (callers already treat empty as generic phrasing), not Danny.
    """
    user_id = _effective_user_id(user_id)
    if user_id:
        try:
            return load_settings(user_id).name or ""
        except Exception:
            pass
        return ""
    return _env_name()


def resolve_timezone(user_id: str | None = None) -> str:
    """The user's IANA timezone name: settings row → env → Asia/Kolkata."""
    user_id = _effective_user_id(user_id)
    if user_id:
        try:
            return load_settings(user_id).timezone or _env_timezone()
        except Exception:
            pass
    return _env_timezone()


def resolve_user_orgs(user_id: str | None = None) -> list[dict]:
    """Routing domains for the classifier/pulse, with is_personal flag.

    M17 privacy guarantee (mirrors resolve_context): once a tenant identity
    is resolved, user_orgs come from THEIR settings row only — a tenant with
    no orgs gets [] (no routing block), never tenant #1's DEFAULT_USER_ORGS.
    The Danny-era defaults serve only unscoped legacy calls (CLI/pre-db/78).
    """
    user_id = _effective_user_id(user_id)
    if user_id:
        try:
            return load_settings(user_id).user_orgs or []
        except Exception:
            pass
        return []
    return list(DEFAULT_USER_ORGS)


def resolve_domains(user_id: str | None = None) -> list[dict]:
    """Backward-compatible alias — returns user_orgs dicts."""
    return resolve_user_orgs(user_id)


def resolve_personal_orgs(user_id: str | None = None) -> list[str]:
    """Backward-compatible alias — returns is_personal org names."""
    user_id = _effective_user_id(user_id)
    if user_id:
        try:
            settings = load_settings(user_id)
            return [d.get("name", "") for d in (settings.user_orgs or []) if d.get("is_personal")]
        except Exception:
            pass
        return []
    return list(DEFAULT_PERSONAL_ORGS)


def resolve_context(user_id: str | None = None) -> str:
    """One-line 'who they are' for prompt slots.

    Privacy guarantee: once a tenant identity is resolved we NEVER fall back
    to DEFAULT_CONTEXT ("Danny (Yashwant Daniel), founder of Crayon…") — a
    tenant with no seeded context gets "" (prompt slot omitted), not tenant
    #1's identity. The Danny-era default only serves unscoped legacy calls.
    """
    user_id = _effective_user_id(user_id)
    if user_id:
        try:
            return load_settings(user_id).context or ""
        except Exception:
            pass
        return ""
    return os.getenv("USER_CONTEXT", DEFAULT_CONTEXT)


def _parse_relationships_row(content: str) -> list[dict]:
    """Parse a per-tenant 'relationships' config row into structured people.

    The row is free-form prose the tenant wrote (e.g. seeded by M6 or edited
    in the DB). Recognized shape — sectioned name lists::

        FAMILY: Sunju (Wife - URGENT/Connection), Jeremy (8), Jaden (5)
        PROFESSIONAL: Team leads at Solvstrat.

    Returns [{name, role, section}] with roles cleaned to a single lowercase
    word ("Wife - URGENT/Connection" → "wife"); numeric parens (ages like
    "(8)" / "(8mo)") collapse to the section name. Robust to missing
    parens, bare names, and arbitrary section labels.
    """
    out: list[dict] = []
    section = ""
    for line in str(content or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ":" in line:
            head, _, body = line.partition(":")
            section = re.sub(r"^\d+\.?\s*", "", head).strip().lower()
            body = body.strip()
        else:
            # Continuation of the previous section ("FAMILY:\nSunju (Wife)\nJeremy").
            if not section:
                continue  # stray text before any section header → garbage
            body = stripped
        if not body:
            continue
        # Trailing period ("Jeffery (8mo).") must not break the entry
        # delimiter — the last entry would otherwise be silently dropped.
        body = body.rstrip(".")
        # Standalone connectors ("Sunju (Wife) and Jeremy") must not drop
        # the preceding entry — normalize to a comma before the regex loop.
        # (A "relationships" row almost never names a two-person entity.)
        body = re.sub(r"\s+(?:and|plus|&)\s+", ", ", body, flags=re.I)
        for m in re.finditer(
            r"([A-Za-z][A-Za-z' .-]*?)\s*(?:\(([^)]*)\))?(?:,|$)", body
        ):
            name = m.group(1).strip().rstrip(".")
            if len(name) < 2:
                continue
            raw_role = (m.group(2) or "").strip()
            role = _clean_relationship_role(raw_role, section)
            out.append({"name": name, "role": role, "section": section})
    return out


def _clean_relationship_role(raw: str, section: str) -> str:
    """Normalize a parenthetical role to one lowercase word.

    "Wife - URGENT/Connection" → "wife"; ages "(8)"/"(8mo)" are not roles
    → the section name ("family"); empty → section name.
    """
    if not raw:
        return section or "family"
    first = re.split(r"[-–—/]", raw, maxsplit=1)[0].strip()
    if not first or re.fullmatch(r"\d+[a-z]*", first):
        return section or "family"
    return first.lower()


def resolve_curated_people(user_id: str | None = None) -> list[dict] | None:
    """Per-tenant curated "who matters" list from core_config 'relationships'.

    The user's own written answer to "who is in my world" (family, close
    friends). The persona synthesis uses this as the authoritative life
    circle; the graph is only the fallback for tenants who never curated a
    row. Fail-closed: no row / empty / parse error → None (caller falls
    back to graph mining). Never another tenant's row.
    """
    user_id = _effective_user_id(user_id)
    if not user_id:
        return None
    try:
        rows = (
            tenant_aware_client()
            .table("core_config")
            .select("content")
            .eq("key", "relationships")
            .limit(1)
            .execute()
        )
        content = (rows.data or [{}])[0].get("content") if rows.data else None
        if not content or not str(content).strip():
            return None
        parsed = _parse_relationships_row(content)
        return parsed or None
    except Exception:
        return None


def resolve_night_signoffs(user_id: str | None = None) -> str:
    """Per-tenant night sign-off options for the classify receipt.

    Reads core_config key 'night_signoffs' (JSON array of quoted phrases)
    under the tenant scope. Tenant-scoped calls never inherit tenant #1's
    personal line — a tenant without a row gets the neutral list. The
    Danny-era default serves only unscoped legacy calls (CLI/pre-db/78).
    """
    user_id = _effective_user_id(user_id)
    if user_id:
        try:
            rows = (
                tenant_aware_client()
                .table("core_config")
                .select("content")
                .eq("key", "night_signoffs")
                .limit(1)
                .execute()
            )
            content = (rows.data or [{}])[0].get("content") if rows.data else None
            if content:
                try:
                    arr = json.loads(content)
                    if isinstance(arr, list) and arr:
                        return " / ".join(f'"{str(s)}"' for s in arr)
                except Exception:
                    pass
        except Exception:
            pass
        return NEUTRAL_NIGHT_SIGNOFFS
    return os.getenv("NIGHT_SIGNOFFS", DEFAULT_NIGHT_SIGNOFFS)


def resolve_vault_url(user_id: str | None = None) -> str:
    """Per-tenant Vault URL for the /vault command (M17).

    Reads core_config key 'vault_url' under the tenant scope. A tenant
    without a row gets "" — commands then reply "not configured", never
    tenant #1's URL. The Danny-era default serves only unscoped legacy calls.
    """
    user_id = _effective_user_id(user_id)
    if user_id:
        try:
            rows = (
                tenant_aware_client()
                .table("core_config")
                .select("content")
                .eq("key", "vault_url")
                .limit(1)
                .execute()
            )
            content = (rows.data or [{}])[0].get("content") if rows.data else None
            return (content or "").strip()
        except Exception:
            return ""
    return os.getenv("VAULT_URL", DEFAULT_VAULT_URL)


def routing_rules_text(user_id: str | None = None) -> str:
    """Render the domain routing rules for the classify prompt.

    Produces one line per domain: keywords → entity, mirroring the pre-M2
    hardcoded PROJECT ROUTING rule so Danny's routing is byte-identical once
    his domains are seeded.
    """
    user_orgs = resolve_user_orgs(user_id)
    lines = []
    for d in user_orgs:
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
