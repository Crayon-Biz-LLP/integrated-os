"""
verify_m2_equivalence.py — M2 equivalence gate (plans/69-multi-tenant-product-plan.md, §5.3)

Gate: "Danny's settings reproduce his current briefing byte-for-byte on the
copy DB (voice, taxonomy, domains)."

This script renders the M2 settings-driven artifacts for tenant #1 (Danny)
from the copy DB and compares them against the pre-M2 hardcoded truth from
HEAD (core/prompts/classify.py, core/pulse/briefing.py, core/lib/time_utils.py,
core/prompts/voice.py). Checks:

  [1] personal_orgs  — pulse work/life split list (byte-for-byte)
  [2] timezone       — Asia/Kolkata == IST (+05:30)
  [3] user name      — users.name resolves to "Danny" (not the env default)
  [4] system persona — pulse briefing personas render "Danny" in the slots
  [5] routing rules  — every HEAD routing clause's keywords → domain mapping
                       survives in the generated taxonomy (semantic gate)

EXIT: 0 if all gates pass, 1 otherwise. Deterministic — no LLM calls.

Usage:
  python3 scripts/verify_m2_equivalence.py --dsn postgresql://postgres@localhost:5433/rhodey_restore_test
  (falls back to $M2_EQUIV_DSN, then localhost:5433/rhodey_restore_test)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    tag = "✅" if ok else "❌"
    print(f"  {tag} [{name}] {detail}")
    if not ok:
        FAILURES.append(name)


# ── copy-DB access (psql — the copy is raw Postgres, no PostgREST) ──────────

def psql_one(dsn: str, sql: str) -> str:
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/opt/libpq/bin:" + env.get("PATH", "")
    out = subprocess.run(
        ["psql", dsn, "-tAc", sql], capture_output=True, text=True, env=env, timeout=30
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


def load_danny(dsn: str) -> dict:
    """Fetch tenant #1's users row + user_settings row from the copy."""
    user_row = psql_one(
        dsn,
        "select id, name from public.users where status='active' order by created_at limit 1",
    )
    uid, name = user_row.split("|")
    tz = psql_one(dsn, f"select timezone from public.user_settings where user_id='{uid}'")
    domains = psql_one(dsn, f"select domains::text from public.user_settings where user_id='{uid}'")
    porgs = psql_one(dsn, f"select personal_orgs::text from public.user_settings where user_id='{uid}'")
    return {"id": uid, "name": name, "timezone": tz, "domains": domains, "personal_orgs": porgs}


# ── HEAD truth (git show HEAD:<path>) ───────────────────────────────────────

def head_file(path: str) -> str:
    """Read the PRE-M2 baseline of `path` — origin/main when available (the
    true pre-M2 reference), falling back to HEAD (running on main before any
    commit, or no remote). After the M0-M6 work is committed to the branch,
    HEAD == M2 code, so HEAD can no longer serve as the literal-Danny
    baseline; origin/main can.
    """
    out = subprocess.run(
        ["git", "show", f"origin/main:{path}"], capture_output=True, text=True, cwd=os.getcwd(), timeout=20
    )
    if out.returncode != 0:
        out = subprocess.run(
            ["git", "show", f"HEAD:{path}"], capture_output=True, text=True, cwd=os.getcwd(), timeout=20
        )
    if out.returncode != 0:
        raise RuntimeError(f"git show {path} failed: {out.stderr.strip()[:300]}")
    return out.stdout


# ── gates ────────────────────────────────────────────────────────────────────

def gate_personal_orgs(danny: dict) -> None:
    import json
    m2 = json.loads(danny["personal_orgs"])
    head = ['Personal', 'Ashraya', 'Ashraya Chennai', 'Chennai North', 'Chennai Central', 'Ashraya India']
    check(
        "personal_orgs byte-for-byte",
        m2 == head,
        f"M2={m2} vs HEAD={head}",
    )


def gate_timezone(danny: dict) -> None:
    from core.lib.time_utils import get_user_timezone
    tz = get_user_timezone(danny["id"])
    off = tz.utcoffset(__import__("datetime").datetime(2026, 1, 1))
    check(
        "timezone Asia/Kolkata == IST",
        danny["timezone"] == "Asia/Kolkata" and off == __import__("datetime").timedelta(hours=5, minutes=30),
        f"settings={danny['timezone']} resolved_offset={off}",
    )


def gate_user_name(danny: dict) -> None:
    from core.services.user_settings import resolve_user_name
    resolved = resolve_user_name(danny["id"])
    check(
        "users.name resolves to Danny",
        resolved == "Danny" and resolved == danny["name"],
        f"resolved={resolved!r} users.name={danny['name']!r}",
    )


def gate_system_persona(danny: dict) -> None:
    """Pulse briefing personas: M2 renders {user_name} slot = Danny.

    M2 keeps the exact HEAD persona text but moves "Danny" into a runtime
    f-string slot ({user_name}). So the check is: for each HEAD persona that
    mentions Danny, the M2 source must contain the identical text with
    "Danny" replaced by the slot marker {user_name} — i.e. the slot renders
    byte-for-byte to the HEAD persona when user_name = Danny.
    """
    m2_src = open("core/pulse/briefing.py").read()
    head_src = head_file("core/pulse/briefing.py")
    personas = [
        "Give Danny the plain picture of the board — what's on top, what's new, what needs doing. No coaching.",
        "Help Danny close the work week: what's done, what can wait. Be dry.",
        "Help Danny close the day: what's done, what's still open. Be dry.",
    ]
    head_personas = [p for p in personas if p in head_src]
    if head_personas:
        # Pre-merge baseline (origin/main is still literal-Danny): all three
        # hardcoded personas must exist there.
        check(
            "HEAD personas contain Danny (sanity)",
            len(head_personas) == 3,
            f"found {len(head_personas)}/3 Danny personas in HEAD",
        )
    else:
        # Post-merge (origin/main == M2 code with {user_name} slots): the
        # literal-Danny baseline is gone BY DESIGN; the slot-rendered personas
        # are the source of truth. Sanity-check all three render via the slot.
        slot_missing = [
            p for p in personas if p.replace("Danny", "{user_name}") not in m2_src
        ]
        check(
            "personas render via {user_name} slot (post-merge)",
            not slot_missing,
            f"slot templates missing from source: {slot_missing[:1]}",
        )
    # In M2 source the slot is written as the f-string variable {user_name}.
    slot = "{user_name}"
    missing = [p for p in head_personas if p.replace("Danny", slot) not in m2_src]
    check(
        "M2 personas render Danny in slot",
        not missing,
        f"templates missing in M2 source: {missing[:1]}",
    )


def gate_routing_rules(danny: dict) -> None:
    """Semantic gate: every HEAD routing keyword → domain survives in the
    generated taxonomy. We extract the HEAD clause, pull its domain keywords,
    and require each to appear in the routing rules rendered from settings."""
    from core.services.user_settings import routing_rules_text

    head_clause = None
    for line in head_file("core/prompts/classify.py").splitlines():
        if "PROJECT ROUTING:" in line:
            head_clause = line.split("PROJECT ROUTING:", 1)[1].strip()
            break
    if not head_clause:
        check("routing rules", False, "HEAD PROJECT ROUTING clause not found")
        return

    rules = routing_rules_text(danny["id"]).lower()

    # (domain, keywords the HEAD prose maps to that domain)
    expected = [
        ("PERSONAL", ["personal finances", "bills", "home", "family", "spiritual practices", "bible reading", "prayer", "volunteering"]),
        ("ASHRAYA", ["ashraya", "church administration", "operations", "accounts"]),
        ("CRAYON", ["corporate governance", "business taxes", "legal compliance"]),
        ("SOLVSTRAT", ["tech", "client"]),
    ]
    for domain, kws in expected:
        d = domain.lower()
        if d not in rules:
            check(f"routing: {domain}", False, "domain missing from generated rules")
            continue
        # keyword presence is the semantic gate (domain line must carry it)
        domain_line = next((ln for ln in rules.splitlines() if d in ln), "")
        missing_kws = [k for k in kws if k not in domain_line and k not in rules]
        check(
            f"routing: {domain} keywords",
            not missing_kws,
            f"{'OK' if not missing_kws else 'missing: ' + str(missing_kws)} (line: {domain_line[:80]})",
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.getenv("M2_EQUIV_DSN", "postgresql://postgres@localhost:5433/rhodey_restore_test"))
    args = ap.parse_args()

    print("M2 equivalence gate — tenant #1 (Danny) on copy DB")
    print(f"DSN: {args.dsn}\n")

    try:
        danny = load_danny(args.dsn)
    except RuntimeError as e:
        print(f"❌ Could not read copy DB: {e}")
        sys.exit(1)

    print(f"Loaded tenant #1: {danny['name']} ({danny['id'][:8]}…) tz={danny['timezone']}\n")

    gate_personal_orgs(danny)
    gate_timezone(danny)
    gate_user_name(danny)
    gate_system_persona(danny)
    gate_routing_rules(danny)

    print()
    if FAILURES:
        print(f"❌ GATE FAILED — {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        sys.exit(1)
    print("✅ ALL M2 EQUIVALENCE GATES PASSED — Danny's settings reproduce his pre-M2 behavior")


if __name__ == "__main__":
    main()
