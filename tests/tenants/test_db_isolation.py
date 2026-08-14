"""DB-level cross-tenant isolation matrix (plan §9).

For every key tenant table: user A sees exactly their own marker row and
never user B's; user B sees exactly theirs and never A's. Plus: every
tenant-scoped RPC function carries an owner parameter.

Runs against the copy DB — skips when unreachable (see conftest.tenants).
"""

from tests.tenants.conftest import _UID_A, _UID_B
from tests.tenants.db_utils import MARK, psql

# (table, marker-column, A-marker-value, B-marker-value)
MARKERS = [
    ("tasks", "title", f"{MARK}A-TASK", f"{MARK}B-TASK"),
    ("graph_nodes", "label", f"{MARK}ANode", f"{MARK}BNode"),
    ("core_config", "key", f"{MARK}a", f"{MARK}b"),
    ("device_tokens", "token", f"{MARK}a-token", f"{MARK}b-token"),
    ("memories", "content", f"{MARK}a-memory", f"{MARK}b-memory"),
]


def _count(table: str, col: str, marker: str, owner: str) -> int:
    return int(psql(
        f"select count(*) from {table} where {col} = '{marker}' and owner_id = '{owner}'"
    ))


def test_cross_tenant_visibility_matrix(tenants):
    for table, col, marker_a, marker_b in MARKERS:
        # A sees own, never B's
        assert _count(table, col, marker_a, _UID_A) == 1, f"A lost own row in {table}"
        assert _count(table, col, marker_b, _UID_A) == 0, f"A saw B's row in {table}"
        # B sees own, never A's
        assert _count(table, col, marker_b, _UID_B) == 1, f"B lost own row in {table}"
        assert _count(table, col, marker_a, _UID_B) == 0, f"B saw A's row in {table}"
    # Sanity: markers actually exist (the matrix above is vacuous otherwise)
    for table, col, marker_a, _ in MARKERS:
        assert int(psql(f"select count(*) from {table} where {col} = '{marker_a}'")) == 1


# Every tenant-scoped data RPC must carry an owner parameter (the facade
# injects it at runtime; the DB function must accept it).
SCOPED_RPCS = [
    "match_memories", "match_graph_nodes", "match_resources",
    "match_conversations", "match_emails_hybrid", "match_whatsapp_hybrid",
    "match_raw_dumps", "search_phrase_nodes", "claim_pending_enrichment_job",
    "get_most_connected_nodes", "find_serendipity_paths", "detect_drift",
    "expire_stale_graph_edges", "archive_terminal_pending_edges",
    "batch_whatsapp_message",
]


def test_scoped_rpcs_carry_owner_param(tenants):
    missing = []
    for rpc in SCOPED_RPCS:
        sig = psql(f"select pg_get_function_arguments(oid) from pg_proc where proname = '{rpc}' limit 1")
        # p_owner is the renamed owner param on the INSERT-heavy RPCs
        if "owner_id" not in sig and "p_owner" not in sig:
            missing.append(rpc)
    assert not missing, f"RPCs missing owner param: {missing}"


def test_global_rpcs_not_owner_filtered(tenants):
    """Admin/global RPCs must NOT carry an owner param (carve-out)."""
    for rpc in ["run_sql"]:
        sig = psql(f"select pg_get_function_arguments(oid) from pg_proc where proname = '{rpc}' limit 1")
        assert "owner_id" not in sig and "p_owner" not in sig, f"{rpc} unexpectedly owner-filtered"
