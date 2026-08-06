#!/usr/bin/env python3
"""
set_user_credit.py — set a TENANT's monthly LLM credit (M6 cost controls v2).

The credit lives on the users table (`users.monthly_credit_usd`) so you can
also edit it directly in the Supabase table editor — this script is the
convenience wrapper + a readout of the current cycle state.

    python scripts/set_user_credit.py --user "Priya" --usd 5
    python scripts/set_user_credit.py --user "Priya" --usd 0 --apply     # clear → default
    python scripts/set_user_credit.py --status --user "Priya"            # cycle + remaining
    python scripts/set_user_credit.py --list                             # all users' credit

The cycle resets on the user's signup day-of-month (users.credit_cycle_day,
default = created_at day). Spend is the llm_spend ledger summed since the
last cycle start. "Remaining" = credit − spent this cycle (floor 0).

Safety: dry-run by default — pass --apply to write to the DB.
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from core.llm.budget import DEFAULT_MONTHLY_CREDIT_USD  # noqa: E402


def _resolve_dsn(args_dsn):
    if args_dsn:
        return args_dsn
    load_dotenv(ROOT / ".env")
    host = os.getenv("SUPABASE_POOLER_HOST")
    db = os.getenv("SUPABASE_DB", "postgres")
    pwd = os.getenv("SUPABASE_DB_PASSWORD") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    user = os.getenv("SUPABASE_DB_USER", "postgres")
    port = os.getenv("SUPABASE_POOLER_PORT", "6543")
    if host and pwd:
        return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"
    return None


def _psql(dsn, sql):
    import subprocess
    r = subprocess.run(["psql", dsn, "-tA", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _sql_literal(s: str) -> str:
    return s.replace("'", "''")


def _find_uid(dsn, name):
    safe = _sql_literal(name)
    row = _psql(dsn, f"select id from public.users where name = '{safe}' limit 1;")
    if not row:
        raise SystemExit(f"user '{name}' not found in public.users")
    return row


def _user_row(dsn, uid):
    return _psql(dsn, (
        f"select u.name, u.monthly_credit_usd, u.credit_cycle_day, "
        f"u.created_at, u.id from public.users u where u.id = '{uid}' limit 1;"
    ))


def main():
    ap = argparse.ArgumentParser(description="Set a tenant's monthly LLM credit")
    ap.add_argument("--user")
    ap.add_argument("--usd", type=float)
    ap.add_argument("--dsn")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    dsn = _resolve_dsn(args.dsn)
    if not dsn:
        raise SystemExit("no DB connection resolved — pass --dsn or set SUPABASE_POOLER_HOST")

    if args.list:
        rows = _psql(dsn, (
            "select u.name, coalesce(u.monthly_credit_usd, 0) from public.users u "
            "where u.status = 'active' order by u.name;"
        ))
        print("monthly credit per user (USD):")
        for line in rows.splitlines() or ["  (none)"]:
            name, cred = line.split("|")
            print(f"  {name}: ${float(cred):.2f}" + ("" if float(cred) else " (default)"))
        print(f"\ndefault when unset: ${DEFAULT_MONTHLY_CREDIT_USD:.2f}/month")
        return

    if not args.user:
        raise SystemExit("need --user NAME (with --usd N or --status)")

    uid = _find_uid(dsn, args.user)

    if args.status or args.usd is None:
        name, cred, cyc_day, created, _uid = _user_row(dsn, uid).split("|")
        credit = float(cred) if cred else DEFAULT_MONTHLY_CREDIT_USD
        day = int(cyc_day) if cyc_day else None
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        cycle_day = day or created_dt.day
        # Cycle start + spend computed against the same DB via SQL. Must mirror
        # core/llm/budget.py cycle_start_utc() EXACTLY: clamp the candidate in
        # the CURRENT month (days-in-month), if that clamped candidate is in
        # the future step back to the previous month's clamped candidate.
        start_sql = (
            f"select make_date(y, m, d) from (\n"
            f"  select\n"
            f"    case when cand <= now() then cy else py end as y,\n"
            f"    case when cand <= now() then cm else pm end as m,\n"
            f"    case when cand <= now() then cd else pd end as d\n"
            f"  from (\n"
            f"    select\n"
            f"      make_date(cy, cm, cd) as cand,\n"
            f"      extract(year from now())::int cy,\n"
            f"      extract(month from now())::int cm,\n"
            f"      least({cycle_day}, extract(day from date_trunc('month', now()) + interval '1 month - 1 day')::int) cd,\n"
            f"      extract(year from date_trunc('month', now()) - interval '1 day')::int py,\n"
            f"      extract(month from date_trunc('month', now()) - interval '1 day')::int pm,\n"
            f"      least({cycle_day}, extract(day from date_trunc('month', now()) - interval '1 day')::int) pd\n"
            f"  ) t\n"
            f") t2;"
        )
        start = _psql(dsn, start_sql)
        spent = _psql(dsn, (
            f"select coalesce(sum(est_cost_usd), 0) from llm_spend "
            f"where owner_id = '{uid}' and ts >= '{start}';"
        ))
        remaining = max(0.0, credit - float(spent))
        print(f"user     : {name}")
        print(f"credit   : ${credit:.2f}/month (set: {cred or 'no (default)'})")
        print(f"cycle    : resets on the {cycle_day}th of each month")
        print(f"cycle    : started {start} (UTC)")
        print(f"spent    : ${float(spent):.4f} this cycle")
        print(f"remaining: ${remaining:.2f}")
        return

    before = _user_row(dsn, uid).split("|")[1] or "(default)"
    if args.usd > 0:
        action = f"set ${args.usd:.2f}/month"
        sql = f"update public.users set monthly_credit_usd = {args.usd:.2f} where id = '{uid}';"
    else:
        action = "clear (back to default)"
        sql = f"update public.users set monthly_credit_usd = null where id = '{uid}';"

    print(f"user      : {args.user} ({uid})")
    print(f"credit    : {action}")
    print(f"before    : {before}")
    if not args.apply:
        print("DRY RUN — pass --apply to write")
        return
    _psql(dsn, sql)
    after = _user_row(dsn, uid).split("|")[1] or "(default)"
    print(f"after     : {after}")


if __name__ == "__main__":
    main()
