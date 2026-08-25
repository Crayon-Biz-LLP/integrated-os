"""Clean up stale (is_current=false) graph_nodes for Danny's tenant.

Uses raw SQL via psql since Supabase API times out on bulk ops.
"""
import subprocess, os, time

OWNER = "c302706e-fe61-422a-b384-68e3bc8f6f8e"

# Get DB connection string from env
os.environ.setdefault("SUPABASE_DB_URL", "")
DB_URL = os.environ.get("SUPABASE_DB_URL", "")

if not DB_URL:
    # Try to construct from supabase URL
    from dotenv import load_dotenv
    load_dotenv()
    DB_URL = os.environ.get("SUPABASE_DB_URL", "")

def run_sql(query, description=""):
    """Run SQL via psql"""
    if description:
        print(f"  {description}...")
    result = subprocess.run(
        ["psql", DB_URL, "-c", query],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        return None
    return result.stdout.strip()

# Step 0: Count before
print("=== Before cleanup ===")
output = run_sql(f"SELECT COUNT(*) as total, COUNT(*) FILTER (WHERE is_current = true) as live, COUNT(*) FILTER (WHERE is_current = false) as stale FROM graph_nodes WHERE owner_id = '{OWNER}'")
print(f"  {output}")

# Step 1: Null supersedes_id references to stale nodes
print("\nStep 1: Nulling supersedes_id references...")
run_sql(f"""
UPDATE graph_nodes
SET supersedes_id = NULL
WHERE supersedes_id IS NOT NULL
  AND owner_id = '{OWNER}'
  AND supersedes_id IN (
    SELECT id FROM graph_nodes WHERE owner_id = '{OWNER}' AND is_current = false
  )
""", "Nullified FK references")

# Step 2: Delete stale nodes
print("\nStep 2: Deleting stale nodes...")
run_sql(f"""
DELETE FROM graph_nodes
WHERE owner_id = '{OWNER}'
  AND is_current = false
""", "Deleted stale nodes")

# Step 3: Count after
print("\n=== After cleanup ===")
output = run_sql(f"SELECT COUNT(*) as total, COUNT(*) FILTER (WHERE is_current = true) as live, COUNT(*) FILTER (WHERE is_current = false) as stale FROM graph_nodes WHERE owner_id = '{OWNER}'")
print(f"  {output}")

# Step 4: Count by type
print("\n=== Remaining nodes by type ===")
output = run_sql(f"SELECT type, COUNT(*) as cnt FROM graph_nodes WHERE owner_id = '{OWNER}' GROUP BY type ORDER BY cnt DESC")
print(f"  {output}")
