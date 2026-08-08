"""
Async PostgreSQL connection pool for Modal.

Replaces PostgREST HTTP calls (~200-500ms each) with direct SQL
over the PostgreSQL wire protocol (~5-15ms each). On a deep query
with 15-20 DB round-trips, this saves ~3-5s.

CONVENTIONS (Phase 2c):
  - READ-ONLY queries only via asyncpg (SELECT, no writes).
  - Writes (INSERT/UPDATE/DELETE) stay on PostgREST.
  - Use async_select / async_select_one for simple column select queries.
  - Use async_fetch / async_fetchrow for complex queries with joins.
  - ALL calls are wrapped in try/except falling back to PostgREST.

Usage:
    Get a connection from the pool:
        conn = await get_conn()
        rows = await conn.fetch("SELECT * FROM tasks WHERE id = $1", task_id)

    Or use convenience helpers:
        rows = await async_fetch("SELECT * FROM tasks WHERE id = $1", task_id)
        row  = await async_fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)

    Phase 2c helpers:
        rows = await async_select("tasks", "id, title", {"status": "todo"}, limit=5)
        row  = await async_select_one("tasks", "id, title", {"id": 123})

    For write operations:
        result = await async_execute(
            "INSERT INTO tasks (title) VALUES ($1) RETURNING id",
            "New task"
        )

    For upserts:
        await async_execute(
            \"\"\"INSERT INTO tasks (title, status)
                VALUES ($1, 'todo')
                ON CONFLICT (dedup_key) DO UPDATE SET title = $1\"\"\",
            title
        )

IMPORTANT:
    - Keep this module as a SUPPLEMENT to core.services.db, not a replacement.
    - The Supabase client (get_supabase()) still works for less-hot paths.
    - Only hot paths (handler pre-work, associative pipeline) use asyncpg.
    - This is a HYBRID migration strategy.
"""

import os
import json
import asyncio
import asyncpg
from typing import Optional
from core.lib.audit_logger import audit_log_sync
from core.services.db import get_tenant

_pool: Optional[asyncpg.Pool] = None
_POOL_CONFIG = {
    "min_size": 1,
    "max_size": 5,
    "command_timeout": 30,  # Pool-level timeout — overridden by per-query wait_for() below
    "max_inactive_connection_lifetime": 300.0,  # 5 min before closing idle conns
}

# Per-query timeout: if asyncpg fails/times out, fail FAST (5s) and fall back to PostgREST.
# The old 30s command_timeout meant every failed asyncpg call added 30s of latency
# before the caller's PostgREST fallback kicked in, EXPLAINING the latency regression.
_ASYNCPG_TIMEOUT = 5.0  # seconds per query


def _resolve_db_credentials() -> tuple[str, str]:
    """W1: prefer the tenant-isolated `rhodey_app` role (db/90+).

    Once RHODEY_APP_DB_PASSWORD is set, asyncpg connects as the NOBYPASSRLS
    app role so Postgres RLS enforces tenant isolation even on the raw-SQL
    path (which bypasses the application facade). Pre-db/90 environments
    (no role, no env var) fall back to the legacy superuser connection so
    existing behavior is preserved until the cutover.
    """
    rls_pw = os.getenv("RHODEY_APP_DB_PASSWORD")
    if rls_pw:
        return os.getenv("RHODEY_APP_DB_USER", "rhodey_app"), rls_pw
    return (
        "postgres",
        os.getenv("SUPABASE_DB_PASSWORD") or os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
    )


async def init_pool() -> asyncpg.Pool:
    """Initialize the global asyncpg connection pool.

    Called once on Modal container startup. Connects as `rhodey_app` when
    RHODEY_APP_DB_PASSWORD is set (RLS-enforced, db/90+); otherwise falls
    back to the legacy postgres role with SUPABASE_DB_PASSWORD (or
    SUPABASE_SERVICE_ROLE_KEY) for backward compat.

    Returns:
        The asyncpg connection pool.
    """
    global _pool
    if _pool is not None:
        return _pool

    supabase_url = os.getenv("SUPABASE_URL")
    db_user, db_password = _resolve_db_credentials()

    if not supabase_url or not db_password:
        raise ValueError(
            "SUPABASE_URL and a database password (SUPABASE_DB_PASSWORD or RHODEY_APP_DB_PASSWORD) must be set"
        )

    # Convert Supabase URL to PostgreSQL connection string
    dsn = _build_pg_dsn(supabase_url, db_user, db_password)

    # Register JSONB codec so asyncpg returns Python dicts instead of strings
    async def _init_conn(conn):
        await conn.set_type_codec(
            'jsonb',
            encoder=json.dumps,
            decoder=json.loads,
            schema='pg_catalog'
        )

    _pool = await asyncpg.create_pool(dsn=dsn, init=_init_conn, **_POOL_CONFIG)
    return _pool


def _build_pg_dsn(supabase_url: str, user: str, password: str) -> str:
    """Convert a Supabase REST URL to a PostgreSQL direct DSN.

    Supabase connection modes:
      1. Direct connection (default):     db.{ref}.supabase.co:5432
      2. Transaction pooler:              aws-{region}.pooler.supabase.com:6543
      3. Session pooler:                  aws-{region}.pooler.supabase.com:5432

    We use DIRECT CONNECTION (port 5432) to avoid Supavisor pooler overhead.
    IPv6 is supported (confirmed via DNS AAAA records).
    Set SUPABASE_USE_POOLER=1 env var to switch back to transaction pooler.

    For the pooler, Supavisor requires the form {user}.{project_ref} (both
    for postgres and for the custom rhodey_app role).

    Handles input formats:
        https://project-ref.supabase.co
        https://project-ref.supabase.co:443

    Returns:
        postgresql://{user}:PASSWORD@db.{ref}.supabase.co:5432/postgres
    """
    # Extract project ref from URL
    # https://abcdefghijklm.supabase.co  →  abcdefghijklm
    host = supabase_url.replace("https://", "").replace("http://", "")
    if ":" in host:
        host = host.split(":")[0]

    if ".supabase.co" in host:
        project_ref = host.split(".")[0]

        # Allow override to pooler via env var
        use_pooler = os.getenv("SUPABASE_USE_POOLER", "").lower() in ("1", "true", "yes")
        if use_pooler:
            pooler_host = os.getenv("SUPABASE_POOLER_HOST")
            if pooler_host:
                pg_host = pooler_host
            else:
                pg_host = f"{project_ref}.pooler.supabase.com"
            pg_port = 6543  # Transaction pooler
            pg_user = f"{user}.{project_ref}"
        else:
            # Direct connection — IPv6 compatible
            pg_host = f"db.{project_ref}.supabase.co"
            pg_port = 5432
            pg_user = user
    else:
        # Fallback for custom hosts — direct connection
        pg_host = host
        pg_port = 5432
        pg_user = user

    return f"postgresql://{pg_user}:{password}@{pg_host}:{pg_port}/postgres"


async def get_pool() -> asyncpg.Pool:
    """Get the global connection pool, initializing if needed.

    Returns:
        The asyncpg connection pool.

    Raises:
        ValueError: If SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY are not set.
    """
    if _pool is None:
        await init_pool()
    return _pool


async def close_pool():
    """Close the global connection pool.

    Should be called on graceful shutdown (Modal handles this automatically).
    """
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── Phase 2c: High-Level SELECT Helpers ──────────────────────────
# These wrap async_fetch with simple SQL generation for common
# read patterns. Column names are hardcoded at call sites (safe).
# Values are parameterized ($1, $2) — no SQL injection risk.


async def async_select(
    table: str,
    columns: str,
    where: Optional[dict] = None,
    order_by: str = None,
    limit: int = None,
) -> list:
    """Simple SELECT via asyncpg. Returns list of dict-like Records.

    Args:
        table: Table name (e.g. 'core_config', 'conversations').
        columns: Column list (e.g. 'key, content', 'id, label').
        where: Equality filters as {column: value} dict.
               Use None for IS NULL checks.
        order_by: Optional ORDER BY clause (e.g. 'created_at DESC').
        limit: Optional LIMIT.

    Returns:
        List of asyncpg Record objects (empty list on error).

    Example:
        rows = await async_select(
            "pending_nodes", "id, label",
            where={"status": "pending", "node_type": "person"},
            limit=10,
        )
    """
    sql = f"SELECT {columns} FROM {table}"
    params = []
    if where:
        clauses = []
        for col, val in where.items():
            if val is None:
                clauses.append(f"{col} IS NULL")
            else:
                clauses.append(f"{col} = ${len(params) + 1}")
                params.append(val)
        sql += " WHERE " + " AND ".join(clauses)
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {limit}"
    return await _safe_fetch(sql, *params)


async def async_select_one(
    table: str,
    columns: str,
    where: Optional[dict] = None,
    order_by: str = None,
) -> Optional[dict]:
    """Fetch a single row via asyncpg. Returns None if no match.

    Args:
        table: Table name.
        columns: Column list.
        where: Equality filters.
        order_by: Optional ORDER BY.

    Returns:
        Single dict-like Record, or None.
    """
    rows = await async_select(table, columns, where=where, order_by=order_by, limit=1)
    return rows[0] if rows else None


async def _tenant_ctx(conn) -> None:
    """W1: stamp the tenant GUC for this transaction (RLS enforcement).

    Runs inside an explicit transaction, so set_config(..., true) is
    transaction-scoped (SET LOCAL semantics). With no tenant context the
    GUC stays unset and Postgres RLS fails closed (0 rows visible).
    """
    uid = get_tenant()
    if uid:
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(uid))


async def _safe_fetch(query: str, *args) -> list:
    """Internal fetch with safety. Returns empty list on any error."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _tenant_ctx(conn)
                return await asyncio.wait_for(
                    conn.fetch(query, *args), timeout=_ASYNCPG_TIMEOUT
                )
    except asyncio.TimeoutError:
        audit_log_sync("asyncpg", "WARNING",
            f"_safe_fetch timed out after {_ASYNCPG_TIMEOUT}s, falling back to PostgREST")
    except Exception as e:
        audit_log_sync("asyncpg", "WARNING",
            f"_safe_fetch failed, falling back to PostgREST: {type(e).__name__}: {e}")
    return []


# ── Low-Level Convenience Helpers ────────────────────────────────


async def async_fetch(query: str, *args) -> list:
    """Fetch multiple rows from the database.

    Wraps the query with a 5s per-query timeout via asyncio.wait_for.
    On timeout/error, logs and returns [] so caller falls back to PostgREST.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _tenant_ctx(conn)
                return await asyncio.wait_for(
                    conn.fetch(query, *args), timeout=_ASYNCPG_TIMEOUT
                )
    except asyncio.TimeoutError:
        audit_log_sync("asyncpg", "WARNING",
            f"async_fetch timed out after {_ASYNCPG_TIMEOUT}s, falling back to PostgREST")
    except Exception as e:
        audit_log_sync("asyncpg", "WARNING",
            f"async_fetch failed, falling back to PostgREST: {type(e).__name__}: {e}")
    return []


async def async_fetchrow(query: str, *args) -> Optional[asyncpg.Record]:
    """Fetch a single row from the database.

    Wraps the query with a 5s per-query timeout via asyncio.wait_for.
    On timeout/error, logs and returns None so caller falls back to PostgREST.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _tenant_ctx(conn)
                return await asyncio.wait_for(
                    conn.fetchrow(query, *args), timeout=_ASYNCPG_TIMEOUT
                )
    except asyncio.TimeoutError:
        audit_log_sync("asyncpg", "WARNING",
            f"async_fetchrow timed out after {_ASYNCPG_TIMEOUT}s, falling back to PostgREST")
    except Exception as e:
        audit_log_sync("asyncpg", "WARNING",
            f"async_fetchrow failed, falling back to PostgREST: {type(e).__name__}: {e}")
    return None


async def async_execute(query: str, *args) -> str:
    """Execute a write query (INSERT/UPDATE/DELETE).

    Args:
        query: SQL query with $1, $2, ... placeholders.
        *args: Values for placeholders.

    Returns:
        Command status string (e.g., "INSERT 0 1").

    RLS note (db/90): as rhodey_app the WITH CHECK policy requires the
    inserted owner_id to match the tenant GUC — a cross-tenant write is
    rejected by the database itself.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _tenant_ctx(conn)
            return await conn.execute(query, *args)


async def async_executemany(query: str, args_list: list) -> None:
    """Execute the same query with multiple parameter sets.

    Args:
        query: SQL query with $1, $2, ... placeholders.
        args_list: List of tuples, each tuple is one set of parameters.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _tenant_ctx(conn)
            await conn.executemany(query, args_list)
