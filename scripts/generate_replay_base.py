#!/usr/bin/env python3
"""generate_replay_base.py — derive db/00_replay_base.sql from a backup.

The db/01..db/101 chain is NOT self-contained: core tables (tasks, memories,
graph_nodes, raw_dumps, …) were created in the Supabase editor BEFORE the
chain began, so no migration creates them. scripts/replay_migrations.py needs
that pre-chain surface to prove the chain applies to a fresh schema.

This generator derives the pre-chain surface from the latest full backup
(backups/rhodey-*.dump, a pg_dump with real column definitions):

    pre-chain tables = backup tables − tables the chain CREATes

plus a small hand-written supplement for tables the chain references that are
absent from the current backup (e.g. `people`/`organizations`, dropped by
db/75 mid-chain but referenced by earlier migrations).

Regenerate after the backup or the chain changes:
    python scripts/generate_replay_base.py     # rewrites db/00_replay_base.sql
    python scripts/generate_replay_base.py --dry-run
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "db"
BACKUP_DIR = ROOT / "backups"
OUT = DB_DIR / "00_replay_base.sql"

# Hand-written constraint supplement: constraints the chain DROPs by name
# without IF EXISTS (db/78 DROP CONSTRAINT core_config_key_key). The backup is
# post-chain (the drop already happened), so these exist only here.
CONSTRAINT_SUPPLEMENT = {
    "core_config": "ALTER TABLE public.core_config ADD CONSTRAINT core_config_key_key UNIQUE (key);",
}


# Hand-written supplement: pre-chain tables NOT in the current backup (dropped
# mid-chain, or dropped before the newest backup) but referenced by early
# migrations. Column sets are exactly what the referencing migrations SELECT/
# reference (db/01's data migrations 1b-1d are the source of truth).
SUPPLEMENT = {
    # people: bigint PK (db/31 comment); chain ADDs is_current/version/
    # supersedes_id (db/31, no IF NOT EXISTS) and organization_name/
    # last_interaction_date/enrichment_notes/enriched_at (db/12) and
    # deleted_at (db/34) — none of those belong in the pre-chain stub.
    "people": [
        "id BIGSERIAL PRIMARY KEY", "name TEXT", "role TEXT",
        "strategic_weight DOUBLE PRECISION", "source TEXT",
        "created_at TIMESTAMPTZ", "graph_node_id UUID",
    ],
    "organizations": ["id uuid DEFAULT extensions.uuid_generate_v4() PRIMARY KEY",
                       "name TEXT UNIQUE",  # db/06 ON CONFLICT (name)
                       "graph_node_id UUID"],  # db/47 triggers, db/74/75 reads
    "email_drafts": ["id BIGSERIAL PRIMARY KEY", "email_id BIGINT"],
    # db/01 1b: whatsapp_messages -> messages
    "whatsapp_messages": [
        "id BIGSERIAL PRIMARY KEY", "sender_name TEXT", "sender_phone TEXT",
        "message_text TEXT", "classification TEXT", "summary TEXT",
        "suggested_title TEXT", "suggested_project TEXT", "has_memory_value BOOLEAN",
        "danny_decision TEXT", "decided_at TIMESTAMPTZ", "shown_in_brief BOOLEAN",
        "embedding vector(768)", "received_at TIMESTAMPTZ", "created_at TIMESTAMPTZ",
        "linked_person_name TEXT",
    ],
    # db/01 1c: call_pending_items -> messages
    "call_pending_items": [
        "id BIGSERIAL PRIMARY KEY", "suggested_title TEXT", "suggested_project TEXT",
        "summary TEXT", "recording_id BIGINT", "danny_decision TEXT",
        "decided_at TIMESTAMPTZ", "shown_in_brief BOOLEAN",
        "possible_duplicate BOOLEAN", "created_at TIMESTAMPTZ", "action_type TEXT",
        "people_mentioned JSONB",
    ],
    # db/01 1d: emails + email_pending_tasks -> messages
    "emails": [
        "id BIGSERIAL PRIMARY KEY", "source TEXT", "direction TEXT",
        "message_id TEXT", "thread_id TEXT", "sender TEXT", "sender_email TEXT",
        "subject TEXT", "body_raw TEXT", "body_summary TEXT", "classification TEXT",
        "linked_person_id BIGINT", "linked_project_id BIGINT",
        "embedding vector(768)", "received_at TIMESTAMPTZ", "created_at TIMESTAMPTZ",
        "gmail_labels JSONB", "status TEXT",
    ],
    # person_aliases: exists in prod (residue scan reads it) but in no
    # migration or backup — created manually post-backup. db/76's pre-check
    # only RAISEs NOTICE when absent (no early exit), so the unguarded
    # backfill at line 95 needs the table to exist; db/76 then drops it.
    "person_aliases": ["id BIGSERIAL PRIMARY KEY", "alias TEXT",
                        "canonical_name TEXT", "resolution_count INTEGER"],
    "email_pending_tasks": [
        "id BIGSERIAL PRIMARY KEY", "email_id BIGINT", "suggested_title TEXT",
        "suggested_project TEXT", "is_human_sender BOOLEAN", "danny_decision TEXT",
        "shown_in_brief BOOLEAN", "possible_duplicate BOOLEAN",
        "duplicate_of_title TEXT", "project_confidence DOUBLE PRECISION",
        "project_mapping_reason TEXT",
    ],
}


def _chain_tables() -> tuple[set[str], dict[str, int], dict[str, int]]:
    """(all, first_created_index, first_referenced_index) per table, in NUMERIC
    migration order (the order the chain is actually applied)."""
    def _num_key(p: Path):
        m = re.match(r"(\d+)", p.name)
        return (int(m.group(1)) if m else 0, p.name)

    files = sorted(
        (f for f in DB_DIR.glob("*.sql")
         if f.name != "00_replay_base.sql" and re.match(r"^\d+", f.name)),
        key=_num_key,
    )
    all_tables: set[str] = set()
    first_created: dict[str, int] = {}
    first_referenced: dict[str, int] = {}
    for idx, p in enumerate(files):
        src = re.sub(r"--[^\n]*", "", p.read_text(encoding="utf-8"))
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        for m in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?([a-z_0-9]+)\s*\(",
            src, re.I):
            t = m.group(1).lower()
            all_tables.add(t)
            first_created.setdefault(t, idx)
        for m in re.finditer(
            r"(?:ALTER\s+TABLE|COMMENT\s+ON\s+(?:COLUMN|TABLE)|REFERENCES|INSERT\s+INTO|UPDATE|DELETE\s+FROM|FROM|JOIN)\s+(?:public\.)?([a-z_0-9]+)",
            src, re.I):
            t = m.group(1).lower()
            if t in ("auth", "storage", "extensions", "supabase_functions"):
                continue
            all_tables.add(t)
            first_referenced.setdefault(t, idx)
    return all_tables, first_created, first_referenced


def _chain_renames() -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """(table_renames {new: old}, column_renames {table: {new_col: old_col}}).

    The backup has post-rename names; the chain's RENAME expects the pre-chain
    name (db/51: project_creation_signals -> org_creation_signals)."""
    table_renames: dict[str, str] = {}
    column_renames: dict[str, dict[str, str]] = {}
    for p in sorted(f for f in DB_DIR.glob("*.sql") if f.name != "00_replay_base.sql"):
        src = re.sub(r"--[^\n]*", "", p.read_text(encoding="utf-8"))
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        for m in re.finditer(
            r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:public\.)?([a-z_0-9]+)\s+RENAME\s+TO\s+(?:public\.)?([a-z_0-9]+)",
            src, re.I):
            table_renames[m.group(2).lower()] = m.group(1).lower()
        for m in re.finditer(
            r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:public\.)?([a-z_0-9]+)\s+RENAME\s+COLUMN\s+([a-z_0-9]+)\s+TO\s+([a-z_0-9]+)",
            src, re.I):
            t = m.group(1).lower()
            column_renames.setdefault(t, {})[m.group(3).lower()] = m.group(2).lower()
    return table_renames, column_renames


def _chain_added_columns() -> dict[str, set[str]]:
    """{table: {columns}} the chain ADDs via ALTER TABLE — the backup (post-chain)
    already has them, so the pre-chain stubs must NOT (the chain adds them)."""
    added: dict[str, set[str]] = {}
    for p in sorted(f for f in DB_DIR.glob("*.sql") if f.name != "00_replay_base.sql"):
        src = re.sub(r"--[^\n]*", "", p.read_text(encoding="utf-8"))
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        for m in re.finditer(
            r"ALTER\s+TABLE\s+(?:public\.)?([a-z_0-9]+)\s+ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([a-z_0-9]+)",
            src, re.I):
            added.setdefault(m.group(1).lower(), set()).add(m.group(2).lower())
    return added


def _backup_unique_constraints() -> list[str]:
    """UNIQUE constraints from the backup's ALTER TABLE ADD CONSTRAINT lines.

    The chain drops some pre-chain constraints BY NAME without IF EXISTS
    (db/78: DROP CONSTRAINT core_config_key_key) — the stub must carry the
    same constraint names. PRIMARY KEY lines are skipped (emit() adds its
    own PK); FK lines are skipped (chain FKs are the source of truth)."""
    dumps = sorted(BACKUP_DIR.glob("rhodey-*.sql")) or sorted(BACKUP_DIR.glob("rhodey-*.dump"))
    if not dumps:
        return []
    out = []
    for line in dumps[-1].read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"ALTER TABLE (?:ONLY )?public\.([a-z_0-9]+) ADD CONSTRAINT ([a-z_0-9_]+) UNIQUE \((.+)\);?$", line.strip())
        if m and "PRIMARY" not in m.group(3):
            out.append((m.group(1).lower(), line.strip()))
    return out


def _backup_tables() -> dict[str, list[str]]:
    """{table: [column lines]} parsed from the newest backup dump."""
    # Prefer plain-format dumps (*.sql — parseable text); the *.dump files
    # are pg_dump custom-format binaries (unreadable as text).
    dumps = sorted(BACKUP_DIR.glob("rhodey-*.sql")) or sorted(BACKUP_DIR.glob("rhodey-*.dump"))
    if not dumps:
        return {}
    tables: dict[str, list[str]] = {}
    current: str | None = None
    for line in dumps[-1].read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"CREATE TABLE (?:public\.)?([a-z_0-9]+) \(", line)
        if m:
            current = m.group(1).lower()
            tables[current] = []
            continue
        if current and line.strip() == ");":
            current = None
            continue
        if current and re.match(r"\s+[a-z_0-9]+\s", line):
            col = line.strip().rstrip(",")
            if not col.startswith(("CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK",
                                   "EXCLUDE", "REFERENCES")):
                tables[current].append(col)
    return tables


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print without writing")
    args = parser.parse_args()

    all_tables, first_created, first_referenced = _chain_tables()
    chain_added = _chain_added_columns()
    table_renames, column_renames = _chain_renames()
    backup_tables = _backup_tables()
    # Pre-chain = referenced BEFORE any CREATE in the chain (or never created
    # by the chain): e.g. pending_enrichment_jobs is referenced by db/36 but
    # only formalized by db/42's CREATE TABLE IF NOT EXISTS — it pre-existed
    # in the editor. A plain "created at all" test would misclassify it.
    pre_chain = {}
    for t, cols in backup_tables.items():
        fc = first_created.get(t)
        fr = first_referenced.get(t)
        if fc is None and t in table_renames:
            # Rename target: the chain may have CREATEd the pre-rename name
            # (db/05 creates project_creation_signals; db/51 renames it to
            # org_creation_signals) — that table is chain-owned, not pre-chain.
            fc = first_created.get(table_renames[t])
        if fc is not None and (fr is None or fr >= fc):
            continue  # genuinely chain-created before any reference
        # The chain may RENAME a pre-chain table (db/51: project_creation_signals
        # -> org_creation_signals): the backup has the post-rename name, but the
        # base must carry the PRE-chain name for the RENAME to apply.
        name = table_renames.get(t, t)
        # Drop columns the chain itself adds — the backup has the post-chain
        # shape; the pre-chain stub must not, or ADD COLUMN fails.
        cols = [c for c in cols if c.split()[0].lower() not in chain_added.get(t, set())]
        # RENAME COLUMN: stub the pre-rename column name (db/51 renames
        # project_name -> org_name; the base must carry project_name).
        col_re = column_renames.get(t, {})
        cols = [f"{col_re[c.split()[0].lower()]} {' '.join(c.split()[1:])}"
                if c.split()[0].lower() in col_re else c for c in cols]
        pre_chain[name] = cols

    # Unique constraints the chain may DROP by name — emitted after all tables.
    unique_lines = []
    for tbl, line in _backup_unique_constraints():
        if tbl in pre_chain and tbl not in SUPPLEMENT:
            unique_lines.append(line)
    lines = [
        "-- db/00_replay_base.sql — REPLAY SCAFFOLD ONLY. NOT A MIGRATION.",
        "--",
        "-- GENERATED by scripts/generate_replay_base.py — DO NOT EDIT BY HAND.",
        "--",
        "-- The db/01..db/101 chain is not self-contained: the core tables were",
        "-- created in the Supabase editor before the chain began, so the chain",
        "-- can only be proven against a fresh schema with this pre-chain base.",
        "-- It is the latest backup's schema MINUS every table the chain itself",
        "-- creates, plus a hand supplement for pre-chain tables dropped",
        "-- mid-chain (people/organizations — db/75) that early migrations",
        "-- reference. Never apply this file to production.",
        "--",
        f"-- Derived from backup: {(sorted(BACKUP_DIR.glob('rhodey-*.sql')) or sorted(BACKUP_DIR.glob('rhodey-*.dump')))[-1].name if backup_tables else '(none found)'}",
        f"-- Pre-chain tables: {len(pre_chain)} from backup + {len(SUPPLEMENT)} supplement",
        "",
    ]
    def emit(table: str, cols: list[str]) -> None:
        nonlocal lines
        cols = list(cols) or ["id BIGSERIAL PRIMARY KEY"]
        # The backup defines PRIMARY KEYs via separate ALTER TABLE lines that
        # this parser drops — re-add a PK on the id column so FK REFERENCES
        # from the chain resolve (chain FKs target id on pre-chain tables).
        first = cols[0]
        idm = re.match(r"^id\s+(integer|bigint|int8|bigserial|serial)\s*(NOT NULL)?\s*$", first, re.I)
        if "PRIMARY KEY" not in cols[0] and idm:
            # Identity creates the {table}_id_seq the chain GRANTs on
            # (e.g. db/42 GRANT USAGE ON SEQUENCE pending_enrichment_jobs_id_seq).
            cols[0] = f"id {idm.group(1).lower()} GENERATED ALWAYS AS IDENTITY PRIMARY KEY"
        elif "PRIMARY KEY" not in cols[0] and re.match(r"^id\s+(bigint|bigserial|uuid|int8|integer)", first, re.I):
            cols[0] = f"{first.rstrip(',')} PRIMARY KEY"
        lines.append(f"CREATE TABLE public.{table} (")
        lines += [f"    {c}," for c in cols]
        lines[-1] = lines[-1].rstrip(",")
        lines.append(");")
        lines.append("")

    # Supplement wins where present: for tables the chain ALTERs from a
    # PRE-chain shape, the backup (post-chain) column set is the WRONG shape
    # (e.g. email_drafts: backup has old_email_id, but db/01 renames email_id
    # → old_email_id, so the pre-chain table must have email_id).
    for table, cols in sorted(SUPPLEMENT.items()):
        emit(table, cols)
        pre_chain.pop(table, None)
    for table in sorted(pre_chain):
        emit(table, pre_chain[table])
    # Unique constraints the chain may DROP by name (db/78 core_config_key_key)
    # — emitted after all tables, backup-derived tables only.
    for tbl, line in _backup_unique_constraints():
        if tbl in pre_chain and tbl not in SUPPLEMENT:
            lines.append(line)
            lines.append("")
    for line in CONSTRAINT_SUPPLEMENT.values():
        lines.append(line)
        lines.append("")

    text = "\n".join(lines)
    if args.dry_run:
        print(text[:2000])
        print(f"\n… {len(lines)} lines, would write to {OUT}")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} — {len(pre_chain)} backup-derived + {len(SUPPLEMENT)} supplement tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
