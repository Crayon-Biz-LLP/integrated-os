#!/usr/bin/env python3
"""scan_tenant1_residue.py — the personal-residue gate (M17).

Why this exists: every tenant-1 leak we found (Danny's name in other
tenants' briefings, church/ministry prompt content, the vault URL, family
names in few-shot examples, "go be a dad") was found by a MANUAL grep with
different patterns each time. This script makes that class of bug a CI
failure instead of a discovery: it derives tenant personal identifiers from
the live DB and fails if any of them appear in shared runtime code.

Blocklist sources (live DB, best-effort):
  - every user's display name (users.name)
  - every person alias (person_aliases.alias / canonical_name)
  - every routing domain name + personal org (user_settings.domains /
    personal_orgs, all tenants)
  - every core_config 'vault_url' value
  - a committed static supplement (known tenant-1 world: names, orgs,
    flavor words, URLs) so the gate still works when the DB is unreachable
    (CI without secrets) and catches things not yet in the DB.

Scanned trees: core/, api/, rhodey_app/lib/, frontend/src/ — every place
whose strings reach another tenant's screen or model call. NOT scanned:
tests/ (goldens are tenant-1's by design), scripts/ (seed/migration scripts
legitimately reference tenant 1), db/, docs/, plans/, session-notes/.

Allowlist (whole-file / whole-token, documented below): the two files that
INTENTIONALLY carry the Danny-era fallback constants (user_settings.py,
briefing_sections.py) and the legacy `danny_decision` column name.

Exit code: 0 = clean, 1 = residues found (CI fails). Run: `--offline` skips
the live-DB derivation (uses the static supplement only).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── Static supplement (committed) ──────────────────────────────────────────
# Known tenant-1 world: names, organizations, URLs, flavor tokens. This list
# is intentionally tight — the LIVE DB derivation is the primary source; this
# is the fallback + catches non-DB words (like "dad" sign-offs, "ministry").
STATIC_BLOCKLIST: list[str] = [
    # names (Danny + his world)
    "Danny", "Daniel", "Yashwant", "Sunjula", "Sunju", "Marcus", "Binu",
    "Anita", "Amita", "Vasanth", "Yasir", "Kiara", "Kumar", "Abhishek",
    "Paulsons", "Jaden", "Jeffery", "Joel", "Amma", "The Boys",
    # organizations / domains / clients
    "Crayon", "Solvstrat", "Qhord", "Ashraya", "Atna", "Equisoft",
    # vault / URLs
    "danny-integrated-os", "danielyashwant",
    # flavor words that leaked into shared prompts (now removed; no regressions)
    "ministry", "prayer meeting", "go be a dad", "Elder Thomas",
]

# Common English words that appear as graph labels / domain names — they are
# NOT evidence of a leak (a tenant can legitimately have a "Family" domain).
STOPLIST: set[str] = {
    "work", "home", "personal", "family", "finance", "finances", "business",
    "ideas", "schedule", "done", "team", "school", "health", "test", "church",
    "volunteer", "volunteers", "event", "family member", "community", "prayer",
    "strategy", "operations", "accounts", "admin", "company", "client",
    "vendor", "product", "tech", "platform", "middleware", "governance",
    "legal", "tax", "bills", "insurance", "banking", "corporate",
}

# Whole-file allowlist — files that INTENTIONALLY carry tenant-1's fallback
# constants (documented M6/M17 design), or Danny-only channels whose prompts
# are hardcoded to his world (call/WhatsApp ingest — explicitly not released
# to other tenants; future work: data-drive them when released).
ALLOW_FILES: set[str] = {
    "core/services/user_settings.py",       # Danny-era fallback constants
    "core/services/briefing_sections.py",   # Danny default row (M9.3 fail-closed)
    "core/services/briefing_schedule.py",   # preset comments only
    "core/lib/constants.py",                # github org handle config
    "core/skills/archive_ingest.py",        # TENANT1_* constants — Danny-channel config (M6)
    "core/skills/call_ingest.py",           # Danny-only channel (his Drive) — static routing prompt
    "core/skills/whatsapp_ingest.py",       # Danny-only channel — static routing prompt
    "core/retrieval/seed_eval_gold.py",     # tenant-1 seed source (his eval-gold rows)
}

# Whole-token allowlist — identifiers that are schema/history, not content.
ALLOW_TOKENS: set[str] = {"danny_decision", "danny_decision_val"}

# (rel_path, token) allowlist — runtime content that is legitimately tenant-1
# branded and never reaches another tenant: the pinned admin dashboard (serves
# tenant #1's OWN world), the product's public API endpoint (Modal account
# name), and the Android applicationId (product branding).
ALLOW_PAIRS: set[tuple[str, str]] = {
    # Product endpoint / package — shared infrastructure, not tenant data.
    ("api/index.py", "danielyashwant"),
    ("rhodey_app/lib/services/api_config.dart", "danielyashwant"),
    # Settings hint shows the same product endpoint as api_config.dart's
    # defaultBaseUrl (which is allowed above) — shared infrastructure, not
    # another tenant's experience. KEEP IN SYNC: if defaultBaseUrl is ever
    # neutralized to a per-tenant placeholder, this hint must change too.
    ("rhodey_app/lib/screens/settings_screen.dart", "danielyashwant"),
    ("rhodey_app/lib/services/widget_data_provider.dart", "Crayon"),
    ("rhodey_app/lib/services/notification_service.dart", "Crayon"),
    ("rhodey_app/lib/services/update_service.dart", "Crayon"),
    # Pinned admin dashboard — DASHBOARD_OWNER_ID serves tenant #1's world.
    ("frontend/src/app/dashboard/clusters/clusters-shell.tsx", "Ashraya"),
    ("frontend/src/app/dashboard/memories/graph/page.tsx", "Danny"),
    ("frontend/src/app/api/episodes/stream/route.ts", "Danny"),
    ("frontend/src/app/api/graph/ego/route.ts", "Danny"),
    ("frontend/src/components/resources/resource-detail-sheet.tsx", "Ashraya"),
    ("frontend/src/components/decisions/graph-pending-list.tsx", "ministry"),
    ("frontend/src/lib/supabase-server.ts", "Danny"),
    # Legacy root-person resolution fallback — owner-scoped query, fail-closed
    # (tenant #2 resolves via the alias/root-label paths first; the Danny
    # literal only ever matches tenant #1's own graph).
    ("core/lib/graph_rules.py", "Danny"),
    # TENANT1_EMAIL_ARCHIVE_LABEL constant — Danny-channel config (M6); the
    # rest of email_ingest.py is the multi-tenant fan-out file and stays gated.
    ("core/skills/email_ingest.py", "Ashraya"),
    # FALSE POSITIVE (d9a8b1b): entity_detector.py's _COMMON_ORG_WORDS is a
    # generic English vocabulary guard — ordinary words that must NEVER become
    # org entities ('church', 'school', 'family', 'community' are already in
    # STOPLIST). 'ministry' here is generic NLP vocabulary beside them, NOT
    # tenant-1 flavor content; prompts stay gated. Kept as a pair (not STOPLIST)
    # so the token still flags everywhere else.
    ("core/lib/entity_detector.py", "ministry"),
}

# Files whose HITS are always comments/docstrings (kept out of the scan to
# avoid noise — dev-facing text is not another tenant's experience).
SKIP_EXT: set[str] = {".pyc", ".g.dart", ".freezed.dart"}

SCAN_DIRS: list[str] = [
    "core",
    "api",
    "rhodey_app/lib",
    "frontend/src",
]


def _word_pattern(token: str) -> re.Pattern:
    """Word-boundary, case-insensitive match for a blocklist token."""
    escaped = re.escape(token)
    return re.compile(rf"\b{escaped}\b", re.IGNORECASE)


_WORDISH = re.compile(r"^[A-Za-z][A-Za-z0-9 .'_-]{2,}$")


def _clean_tokens(tokens: set[str]) -> list[str]:
    """Keep only word-like tokens >= 3 chars (drop punctuation/single chars)."""
    out = set()
    for t in tokens:
        t = t.strip()
        if not t or t.lower() in STOPLIST:
            continue
        if not _WORDISH.match(t):
            continue
        out.add(t)
    return sorted(out)


def derive_blocklist(offline: bool) -> tuple[list[str], bool]:
    """Live-DB blocklist (users, aliases, domains, personal orgs, vault URL).

    Returns (tokens, live) — live=False when the DB derivation was skipped
    (offline flag or DB failure), in which case the static supplement alone
    is used and the run still fails loudly on it.
    """
    tokens: set[str] = set(STATIC_BLOCKLIST)
    live = False
    if not offline:
        try:
            sys.path.insert(0, str(ROOT))
            from core.services.db import get_supabase, tenant_aware_client

            db = get_supabase()
            for r in db.table("users").select("name").execute().data:
                name = (r.get("name") or "").strip()
                if name and name.lower() not in STOPLIST:
                    tokens.add(name)
            try:
                for r in db.table("person_aliases").select("alias, canonical_name").execute().data:
                    for k in ("alias", "canonical_name"):
                        v = (r.get(k) or "").strip()
                        if v and v.lower() not in STOPLIST:
                            tokens.add(v)
            except Exception:
                pass  # table optional
            for r in db.table("user_settings").select("domains, personal_orgs").execute().data:
                doms = r.get("domains") or []
                if isinstance(doms, str):  # jsonb can come back as a JSON string
                    try:
                        doms = json.loads(doms)
                    except Exception:
                        doms = []
                for dom in doms:
                    n = (dom.get("name") or "").strip() if isinstance(dom, dict) else str(dom).strip()
                    if n:
                        tokens.add(n)
                for org in (r.get("personal_orgs") or []):
                    org = str(org).strip()
                    if org:
                        tokens.add(org)
            try:
                for r in (
                    tenant_aware_client()
                    .table("core_config")
                    .select("content")
                    .eq("key", "vault_url")
                    .execute()
                    .data
                ):
                    url = (r.get("content") or "").strip()
                    if url:
                        tokens.add(url)
            except Exception:
                pass
            live = True
        except Exception as e:  # pragma: no cover - CI without secrets
            print(f"⚠️  DB derivation unavailable ({type(e).__name__}); using static supplement only.")
    return _clean_tokens(tokens), live


_TRIPLE = ('"""', "'''")


def _py_docstring_lines(path: Path) -> set[int]:
    """Return 1-based line numbers that are TRUE docstrings (AST-detected).

    Precise: only the first statement of a module/function/class body is a
    docstring. Prompt TEMPLATES assigned with triple quotes are Assign nodes,
    not docstrings, so they are NOT in this set and stay scanned (the M17
    core fix). Handles multi-line signatures, decorators, async defs, and
    module-level docstrings — the shapes a `prev == def` heuristic misses.
    Returns empty set on parse error (caller falls back to scanning).
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return set()
    lines: set[int] = set()

    def walk(node: ast.AST) -> None:
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                n = body[0]
                lines.update(range(n.lineno, (n.end_lineno or n.lineno) + 1))
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return lines


def _iter_runtime_lines(path: Path):
    """Yield (line_no, text) for runtime content lines.

    Docstrings (module/function — dev-facing prose) are skipped like
    comments. Data triple-quoted strings — prompt TEMPLATES assigned with
    triple quotes — are RUNTIME content and ARE scanned; prompt templates
    are the primary leak surface (this distinction is the M17 gate's core
    fix; the first version skipped them and missed the classify sign-off
    line).
    """
    doc_lines = _py_docstring_lines(path) if path.suffix == ".py" else set()
    in_data: str | None = None
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        stripped = line.lstrip()
        if i in doc_lines:
            continue  # true docstring (AST) — dev-facing prose
        if stripped.startswith(("#", "//", "*")):
            continue
        if in_data is not None:
            yield i, line  # prompt template body — scanned
            if in_data in line:
                in_data = None
            continue
        marker = next((m for m in _TRIPLE if m in line), None)
        if marker is not None:
            # data string (assignment/f-string/return) — runtime content
            if line.count(marker) % 2 == 1:
                in_data = marker
            yield i, line
            continue
        yield i, line


def scan(tokens: list[str]) -> list[tuple[str, int, str, str]]:
    """Return [(rel_path, line_no, token, line)] for every residue found."""
    patterns = [(t, _word_pattern(t)) for t in tokens]
    findings: list[tuple[str, int, str, str]] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_dir() or p.suffix in SKIP_EXT:
                continue
            if any(part in {"__pycache__", ".dart_tool", "build", "node_modules"} for part in p.parts):
                continue
            rel = p.relative_to(ROOT).as_posix()
            if rel in ALLOW_FILES:
                continue
            try:
                for i, line in _iter_runtime_lines(p):
                    for token, pat in patterns:
                        if token.lower() in ALLOW_TOKENS:
                            continue
                        if (rel, token) in ALLOW_PAIRS:
                            continue
                        if pat.search(line):
                            findings.append((rel, i, token, line.strip()[:140]))
                            break
            except Exception:
                continue
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Skip live-DB derivation")
    args = parser.parse_args()

    tokens, live = derive_blocklist(args.offline)
    print(f"🔎 Residue scan — {len(tokens)} blocklist tokens "
          f"({'live DB' if live else 'static supplement only'})")
    findings = scan(tokens)
    if not findings:
        print("✅ CLEAN — no tenant residue in shared runtime code.")
        return 0
    print(f"❌ FOUND {len(findings)} residue hit(s):")
    seen: set[str] = set()
    for rel, line_no, token, line in findings:
        key = f"{rel}:{line_no}"
        if key in seen:
            continue
        seen.add(key)
        print(f"   {rel}:{line_no}  [{token}]  {line}")
    print("\nFix: neutralize the token in shared code (it must come from the")
    print("tenant's DB row, never a literal). If the hit is a FALSE POSITIVE,")
    print("add the file to ALLOW_FILES or the token to ALLOW_TOKENS with a")
    print("comment explaining why it is not another tenant's experience.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
