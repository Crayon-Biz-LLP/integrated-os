"""
briefing_sections.py — M9.3 per-tenant briefing sections (plans/70 §M9.3).

The pulse briefing's board sections were hardcoded to Danny's world
("Work/Home/Church/Done/Schedule/Ideas", "faith" framing). Every tenant gets
the same base skeleton — Schedule · Done · Work · Home · Ideas · Stale Loops —
plus an OPTIONAL per-tenant domain-sections list from core_config
(`briefing_sections` row), so Danny keeps his exact sections and a fresh
tenant gets the clean skeleton.

Design (the user's S3 decision):
    core_config 'briefing_sections' (JSON):
      Danny   → {domain_sections: [Church: "Ashraya admin, operations,
                finance tasks only."], home_description: "Family and personal
                tasks only. Not Ashraya/Church.", role_framing: "work,
                family, and faith"}
      Others  → {} (base skeleton only)

Guarantees:
  1. BYTE-IDENTICAL FOR DANNY — his seeded row reproduces the current prompt
     verbatim (proven by scripts/verify_m9_3_sections.py vs the committed
     baseline tests/golden/briefing_tenant1.txt).
  2. BASE SKELETON FOR EVERYONE — the Work/Home split already derives from
     personal_orgs; domain sections are explicit config, NOT auto-derived
     (auto-derivation would over-generate for Danny).
  3. NEVER-RAISE — any read/parse error falls back to the Danny-era default
     (the byte-identical safety net for the existing single-tenant system);
     never a crash, never a 500.
  4. NO CACHE — the briefing runs a few times a day (cold path), unlike the
     per-message classify path, so a fresh read per briefing is correct and
     keeps config edits immediate (mirrors resolve_root_label).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from core.services.db import (
    maybe_single_safe,
    tenant_aware_client,
    tenant_scope,
)


# ── The Danny-era default (byte-identical pre-seed fallback) ────────────────
# Reproduces the hardcoded prompt exactly. Seeded into core_config by
# scripts/seed_tenant1_m6_config.py; the runtime reads the row when present.
DEFAULT_BRIEFING_SECTIONS: dict = {
    "domain_sections": [
        {"name": "Church", "description": "Ashraya admin, operations, finance tasks only."}
    ],
    "home_description": "Family and personal tasks only. Not Ashraya/Church.",
    "role_framing": "work, family, and faith",
}

# The neutral fallback for a NEW tenant born without a seeded row — base
# skeleton only, no domain sections, no faith framing.
NEUTRAL_BRIEFING_SECTIONS: dict = {
    "domain_sections": [],
    "home_description": "Family and personal tasks only.",
    "role_framing": "work and personal life",
}

# Base skeleton constants — identical for every tenant.
_BOARD_BASE = [
    "Schedule: Calendar events today only.",
    "Done: Recently completed/closed tasks from SYSTEM TASKS only.",
    "Work: Active work tasks from SYSTEM TASKS only.",
]
_BOARD_TAIL = [
    "Ideas: ONLY from NEWLY ENRICHED RESOURCES or RECENT LIBRARY PATTERNS. Never from Hindsight or Canonical Pages.",
    "Stale Loops: If STALE_TASKS has items, include with day count. Max 5.",
]


@dataclass(frozen=True)
class BriefingSections:
    """The per-tenant section block pieces injected into the briefing prompt."""

    board_lines: str          # the "- X: desc" section lines (base + domains)
    fidelity_names: str       # "Work/Home/Church/Done" (DATA FIDELITY rule 1)
    urgent_hide: str          # "Home, Church, Ideas" (URGENT mode override)
    night_order: str          # "Schedule, Done, Home, Church, Work (top 2-3), Ideas"
    role_framing: str         # "work, family, and faith" (ROLE line)


def _parse_cfg(row_content: str | None) -> dict:
    """Parse a briefing_sections row; validate shape; never raises."""
    if not row_content:
        return dict(NEUTRAL_BRIEFING_SECTIONS)
    try:
        parsed = json.loads(row_content) if isinstance(row_content, str) else row_content
    except Exception:
        return dict(NEUTRAL_BRIEFING_SECTIONS)
    if not isinstance(parsed, dict):
        return dict(NEUTRAL_BRIEFING_SECTIONS)
    out = dict(NEUTRAL_BRIEFING_SECTIONS)
    if isinstance(parsed.get("domain_sections"), list):
        domains = []
        for d in parsed["domain_sections"]:
            if isinstance(d, dict) and d.get("name") and d.get("description"):
                domains.append({"name": str(d["name"]).strip(), "description": str(d["description"]).strip()})
        out["domain_sections"] = domains
    if isinstance(parsed.get("home_description"), str) and parsed["home_description"].strip():
        out["home_description"] = parsed["home_description"].strip()
    if isinstance(parsed.get("role_framing"), str) and parsed["role_framing"].strip():
        out["role_framing"] = parsed["role_framing"].strip()
    return out


def _fetch_briefing_sections_cfg() -> dict | None:
    """Read the tenant's briefing_sections row from core_config.

    Returns the raw row content (str) or None. Owner-scoped via the facade
    (which reads the ACTIVE tenant context — set by the caller or by
    resolve_briefing_sections' tenant_scope); legacy (unscoped) callers get
    None → the Danny-era default below.
    """
    try:
        cfg = maybe_single_safe(
            tenant_aware_client().table("core_config").select("content").eq("key", "briefing_sections")
        )
        if cfg and cfg.data and cfg.data.get("content") is not None:
            return str(cfg.data["content"])
    except Exception:
        pass
    return None


def _resolve_impl() -> BriefingSections:
    """Build the section block from config — never raises.

    Row absent → the Danny-era default (byte-identical with the current
    hardcoded prompt for the existing single-tenant system); row present but
    empty → the neutral skeleton. Any read/parse failure also falls back to
    the Danny-era default (pre-M0 behavior) — never a crash, never a 500.
    """
    try:
        raw = _fetch_briefing_sections_cfg()
    except Exception:
        raw = None

    if raw is None:
        cfg = dict(DEFAULT_BRIEFING_SECTIONS)
    else:
        cfg = _parse_cfg(raw)

    domains = cfg.get("domain_sections") or []
    domain_names = [d["name"] for d in domains]
    domain_lines = [f"- {d['name']}: {d['description']}" for d in domains]

    board_lines = "\n".join(
        ["- " + line for line in _BOARD_BASE]
        + [f"- Home: {cfg['home_description']}"]
        + domain_lines
        + ["- " + line for line in _BOARD_TAIL]
    )

    fidelity_names = "Work/Home" + (("/" + "/".join(domain_names)) if domain_names else "") + "/Done"
    urgent_hide = "Home" + ((", " + ", ".join(domain_names)) if domain_names else "") + ", Ideas"
    night_order = "Schedule, Done, Home" + ((", " + ", ".join(domain_names)) if domain_names else "") + ", Work (top 2-3), Ideas"

    return BriefingSections(
        board_lines=board_lines,
        fidelity_names=fidelity_names,
        urgent_hide=urgent_hide,
        night_order=night_order,
        role_framing=cfg["role_framing"],
    )


def resolve_briefing_sections(user_id: str | None = None) -> BriefingSections:
    """The tenant's briefing section block (plans/70 §M9.3).

    Scoping: production callers pass nothing and the read uses the ACTIVE
    tenant context (M4 per-tenant pulse runs inside tenant_scope). When
    user_id IS passed, the read is scoped to that tenant explicitly via
    tenant_scope — a caller can never silently read another tenant's row
    because the ambient context differs. Never raises (see _resolve_impl).
    """
    if user_id:
        with tenant_scope(user_id):
            return _resolve_impl()
    return _resolve_impl()


# Re-exported for scripts that need the constants (seed script single source).
def default_briefing_sections_json() -> str:
    """JSON of the Danny-era default, for the seed script (no drift)."""
    return json.dumps(DEFAULT_BRIEFING_SECTIONS, ensure_ascii=False)


def neutral_briefing_sections_json() -> str:
    """JSON of the neutral default, for bootstrap (no drift)."""
    return json.dumps(NEUTRAL_BRIEFING_SECTIONS, ensure_ascii=False)
