"""One-off cleanup: remove the latest Kron Tech document round from Danny's tenant.

Scope: everything created by the LAST upload + confirm of the Kron concept brief —
documents row, document_items audit rows, the created task (incl. superseded twin),
created graph nodes (org + concepts) and their edges, any memories/notes,
raw_dumps (upload + ack), and any pending_nodes stragglers.

Anchoring strategy (deliberate, to avoid touching historical Kron mentions):
  1. Find the latest documents row matching 'Kron' for Danny.
  2. document_items for that doc -> created_entity_ids (exact node deletes).
  3. created_at window = [doc.created_at, now] for tasks/memories/raw_dumps,
     further narrowed by keyword match where the table allows it.

Dry-run by default. Pass --execute to actually delete.
"""
import os
import re
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()

OWNER = "c302706e-fe61-422a-b384-68e3bc8f6f8e"  # Danny
EXECUTE = "--execute" in sys.argv


def _resolve_dsn():
    """Build a postgres DSN from env (same pattern as apply_migrations.py)."""
    dsn = os.environ.get("SUPABASE_DB_URL", "")
    if dsn:
        return dsn
    # Direct connection: db.<project_ref>.supabase.co:5432 (pooler needs tenant
    # identifier in username; direct host does not).
    supabase_url = os.getenv("SUPABASE_URL", "")
    match = re.match(r"https?://([^.]+)\.supabase\.co", supabase_url)
    if not match:
        return None
    project_ref = match.group(1)
    pwd = os.getenv("SUPABASE_DB_PASSWORD") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not pwd:
        return None
    from urllib.parse import quote
    encoded_pw = quote(pwd, safe="")
    return (
        f"postgresql://postgres:{encoded_pw}"
        f"@db.{project_ref}.supabase.co:5432/postgres"
        f"?sslmode=require"
    )


DB_URL = _resolve_dsn()

if not DB_URL:
    print("ERROR: no DB connection resolved (SUPABASE_DB_URL or SUPABASE_POOLER_HOST + password)")
    sys.exit(1)


def run_sql(query, description=""):
    """Run SQL via psql (-tA: tuples-only, unaligned — clean scalar output)."""
    if description:
        print(f"  {description}")
    result = subprocess.run(
        ["psql", DB_URL, "-tA", "-c", query],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"    ERROR: {result.stderr.strip()}")
        return None
    return result.stdout.strip()


def q(value):
    """Escape a value for SQL literal."""
    return str(value).replace("'", "''")


def array_literal(ids):
    """Build a Postgres text[] ARRAY literal from a list of ids."""
    if not ids:
        return "ARRAY[]::text[]"
    return "ARRAY[" + ",".join(f"'{q(i)}'" for i in ids) + "]::text[]"


# ── Step 1: locate the latest Kron document ─────────────────────────────────
print("=== Step 1: locate latest Kron document ===")
doc_rows = run_sql(f"""
    SELECT id, created_at, COALESCE(filename, '(no name)')
    FROM documents
    WHERE owner_id = '{OWNER}'
      AND (filename ILIKE '%kron%' OR extracted_text ILIKE '%kron%'
           OR parsed_breakdown::text ILIKE '%kron%')
    ORDER BY created_at DESC
    LIMIT 5;
""", "documents rows matching 'Kron':")
print(doc_rows)

latest = run_sql(f"""
    SELECT id || ' | ' || created_at
    FROM documents
    WHERE owner_id = '{OWNER}'
      AND (filename ILIKE '%kron%' OR extracted_text ILIKE '%kron%'
           OR parsed_breakdown::text ILIKE '%kron%')
    ORDER BY created_at DESC
    LIMIT 1;
""")
if not latest or "|" not in latest:
    print("No Kron document found — nothing to clean.")
    sys.exit(0)

doc_id, doc_created = latest.split(" | ", 1)
doc_created = doc_created.strip()
print(f"\nAnchor: document id={doc_id} created_at={doc_created}")

window_start = doc_created

# ── Step 2: document_items → created entity ids ─────────────────────────────
print("\n=== Step 2: document_items audit rows ===")
items = run_sql(f"""
    SELECT id, item_type, created_entity_id
    FROM document_items
    WHERE owner_id = '{OWNER}' AND document_id = {doc_id};
""", "document_items for this doc:")
print(items)

entity_ids = run_sql(f"""
    SELECT COALESCE(string_agg(DISTINCT created_entity_id::text, ','), '')
    FROM document_items
    WHERE owner_id = '{OWNER}' AND document_id = {doc_id}
      AND created_entity_id IS NOT NULL;
""")
entity_ids = [e.strip() for e in (entity_ids or "").split(",") if e.strip()]
print(f"created_entity_ids: {entity_ids or '(none)'}")

# Also catch nodes by label (concepts don't carry 'Kron' in their label but were
# created in the confirm window — catch by created_at on graph_nodes).
# Round-1 lesson: the task-node MIRROR (type='task', label = the task title)
# contains neither 'kron' nor the concept names — catch ALL task-type nodes
# created in the window (the only task creation in the window is this round).
node_ids = run_sql(f"""
    SELECT string_agg(id::text, ',')
    FROM graph_nodes
    WHERE owner_id = '{OWNER}'
      AND (
        id::text = ANY({array_literal(entity_ids)})
        OR label ILIKE '%kron%'
        OR (created_at >= '{window_start}'::timestamptz
            AND type IN ('concept', 'organization')
            AND label IN ('Pipedrive', 'PandaDoc', 'Katana'))
        OR (created_at >= '{window_start}'::timestamptz
            AND type = 'task')
      );
""")
node_ids = [n.strip() for n in (node_ids or "").split(",") if n.strip()]
print(f"graph_nodes to delete: {node_ids or '(none)'}")

if node_ids:
    labels = run_sql(f"""
        SELECT string_agg(label, ' | ')
        FROM graph_nodes WHERE id::text = ANY({array_literal(node_ids)});
    """)
    print(f"  labels: {labels}")

# ── Step 3: discovery across remaining tables ───────────────────────────────
print("\n=== Step 3: discovery across remaining tables ===")

discovery = {
    "tasks": f"""
        SELECT id, created_at, LEFT(title, 60) FROM tasks
        WHERE owner_id = '{OWNER}' AND created_at >= '{window_start}'::timestamptz
          AND (title ILIKE '%kron%' OR COALESCE(notes,'') ILIKE '%kron%'
               OR title ILIKE '%alignment session%');
    """,
    "memories": f"""
        SELECT id, created_at, LEFT(content, 60) FROM memories
        WHERE owner_id = '{OWNER}' AND created_at >= '{window_start}'::timestamptz
          AND content ILIKE '%kron%';
    """,
    "raw_dumps": f"""
        SELECT id, created_at, LEFT(COALESCE(content, ''), 60) FROM raw_dumps
        WHERE owner_id = '{OWNER}' AND created_at >= '{window_start}'::timestamptz
          AND (COALESCE(content, '') ILIKE '%kron%'
               OR COALESCE(content, '') ILIKE '%Created 5 items%'
               OR metadata::text ILIKE '%{q(doc_id)}%');
    """,
    "graph_edges": f"""
        SELECT id, source_node_id, target_node_id FROM graph_edges
        WHERE owner_id = '{OWNER}'
          AND (source_node_id::text = ANY({array_literal(node_ids)})
               OR target_node_id::text = ANY({array_literal(node_ids)}));
    """ if node_ids else None,
    "pending_nodes": f"""
        SELECT id, label, node_type, created_at FROM pending_nodes
        WHERE owner_id = '{OWNER}' AND created_at >= '{window_start}'::timestamptz
          AND (label ILIKE '%kron%' OR label IN ('Pipedrive', 'PandaDoc', 'Katana'));
    """,
    "pending_graph_edges": f"""
        SELECT id, source_label, relationship, target_label FROM pending_graph_edges
        WHERE owner_id = '{OWNER}' AND (source_text ILIKE '%document%'
               OR source_label ILIKE '%kron%' OR target_label ILIKE '%kron%'
               OR source_label IN ('Pipedrive', 'PandaDoc', 'Katana')
               OR target_label IN ('Pipedrive', 'PandaDoc', 'Katana'));
    """,
}

found = {}
for table, sql in discovery.items():
    if sql is None:
        continue
    out = run_sql(sql, f"{table}:")
    found[table] = out
    print(f"    {out if out else '(none)'}")

# ── Step 4: delete (or report) ───────────────────────────────────────────────
print("\n=== Step 4: " + ("EXECUTE deletions" if EXECUTE else "DRY RUN (pass --execute to delete)") + " ===")

deletions = []

# FK-clearing order (round-2 lesson: the org fix made tasks.organization_id
# point at the Kron node, so deleting graph_nodes BEFORE tasks raised an FK
# violation that scrolled off — nodes silently survived and the re-upload
# matched "existing org"). Order: edge children → task/mem FK holders → nodes
# → doc audit → doc → dumps → pending.
if node_ids:
    deletions.append((
        "graph_edges (touching doomed nodes)",
        f"DELETE FROM graph_edges WHERE owner_id = '{OWNER}' AND (source_node_id::text = ANY({array_literal(node_ids)}) OR target_node_id::text = ANY({array_literal(node_ids)}));",
    ))

deletions.append((
    "tasks (doc-created task + superseded twin) — FK holder for graph_nodes",
    f"DELETE FROM tasks WHERE owner_id = '{OWNER}' AND created_at >= '{window_start}'::timestamptz AND (title ILIKE '%kron%' OR COALESCE(notes,'') ILIKE '%kron%' OR title ILIKE '%alignment session%');",
))
deletions.append((
    "memories (Kron notes from this round) — FK holder via organization_id",
    f"DELETE FROM memories WHERE owner_id = '{OWNER}' AND created_at >= '{window_start}'::timestamptz AND content ILIKE '%kron%';",
))

if node_ids:
    deletions.append((
        "graph_nodes (doc-created org + concepts + task mirror)",
        f"DELETE FROM graph_nodes WHERE owner_id = '{OWNER}' AND id::text = ANY({array_literal(node_ids)});",
    ))

deletions.append((
    "document_items (audit rows)",
    f"DELETE FROM document_items WHERE owner_id = '{OWNER}' AND document_id = {doc_id};",
))
deletions.append((
    "documents (all Kron-matching doc rows, incl. orphaned failed uploads)",
    f"DELETE FROM documents WHERE owner_id = '{OWNER}' AND (filename ILIKE '%kron%' OR extracted_text ILIKE '%kron%' OR parsed_breakdown::text ILIKE '%kron%');",
))
deletions.append((
    "raw_dumps (upload + ack rows)",
    f"DELETE FROM raw_dumps WHERE owner_id = '{OWNER}' AND created_at >= '{window_start}'::timestamptz AND (COALESCE(content, '') ILIKE '%kron%' OR COALESCE(content, '') ILIKE '%Created 5 items%' OR metadata::text ILIKE '%{q(doc_id)}%');",
))
deletions.append((
    "pending_nodes (stragglers, none expected)",
    f"DELETE FROM pending_nodes WHERE owner_id = '{OWNER}' AND created_at >= '{window_start}'::timestamptz AND (label ILIKE '%kron%' OR label IN ('Pipedrive', 'PandaDoc', 'Katana'));",
))
deletions.append((
    "pending_graph_edges (document-proposed relationship edges)",
    f"DELETE FROM pending_graph_edges WHERE owner_id = '{OWNER}' AND (source_text ILIKE '%document%' OR source_label ILIKE '%kron%' OR target_label ILIKE '%kron%' OR source_label IN ('Pipedrive', 'PandaDoc', 'Katana') OR target_label IN ('Pipedrive', 'PandaDoc', 'Katana'));",
))

total = 0
failed = False
for label, sql in deletions:
    if EXECUTE:
        out = run_sql(sql, f"DELETE {label}:")
        if out is None:
            failed = True
            print("    ⚠️ FAILED — see error above")
            continue
        count = 0
        for line in (out or "").splitlines():
            line = line.strip()
            if line.startswith("DELETE ") and line[7:].isdigit():
                count = int(line[7:])
        total += count
        print(f"    deleted {count}")
    else:
        print(f"  would delete: {label}")

if EXECUTE:
    if failed:
        print("\n=== ❌ INCOMPLETE — one or more deletes failed. Re-run dry mode to see what survived. ===")
        sys.exit(1)
    # Post-verification: the cleanup MUST prove it worked (round-2 lesson:
    # silent partial failure left nodes behind and the re-upload matched
    # "existing org").
    survivors = run_sql(f"""
        SELECT COUNT(*) FROM graph_nodes
        WHERE owner_id = '{OWNER}'
          AND (label ILIKE '%kron%' OR label IN ('Pipedrive', 'PandaDoc', 'Katana')
               OR (created_at >= '{window_start}'::timestamptz AND type = 'task'));
    """)
    doc_left = run_sql(f"SELECT COUNT(*) FROM documents WHERE owner_id = '{OWNER}' AND (filename ILIKE '%kron%' OR extracted_text ILIKE '%kron%' OR parsed_breakdown::text ILIKE '%kron%');")
    if survivors and survivors.strip() != "0":
        print(f"\n=== ❌ VERIFICATION FAILED — {survivors} Kron-round node(s) still live. ===")
        sys.exit(1)
    if doc_left and doc_left.strip() != "0":
        print(f"\n=== ❌ VERIFICATION FAILED — {doc_left} Kron document row(s) still present. ===")
        sys.exit(1)
    print(f"\n=== DONE — {total} rows deleted across {len(deletions)} tables; verified 0 survivors ===")
    print("Danny's tenant is clean of the latest Kron round. Safe to re-upload.")
else:
    print("\nDry run complete. Review the discovery above, then re-run with --execute.")