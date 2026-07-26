"""
Async PostgreSQL connection pool for Modal.

Replaces PostgREST HTTP calls (~200-500ms each) with direct SQL
over the PostgreSQL wire protocol (~5-15ms each). On a deep query
with 15-20 DB round-trips, this saves ~3-5s.

⚠️ CRITICAL: Supabase uses PgBouncer (connection pooler) in
transaction mode. Prepared statements are NOT supported through
PgBouncer. We set statement_cache_size=0 to prevent the
"prepared statement does not exist" crash.

Usage:
    Get a connection from the pool:
        conn = await get_conn()
        rows = await conn.fetch("SELECT * FROM tasks WHERE id = $1", task_id)

    Or use convenience helpers:
        rows = await async_fetch("SELECT * FROM tasks WHERE id = $1", task_id)
        row  = await async_fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)

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
import asyncpg
from typing import Optional

_pool: Optional[asyncpg.Pool] = None
_POOL_CONFIG = {
    "min_size": 1,
    "max_size": 5,
    "statement_cache_size": 0,  # Required for PgBouncer compatibility
    "command_timeout": 30,
    "max_inactive_connection_lifetime": 300.0,  # 5 min before closing idle conns
}


async def init_pool() -> asyncpg.Pool:
    """Initialize the global asyncpg connection pool.

    Called once on Modal container startup. Uses the Supabase
    connection string and service role key for auth.

    Returns:
        The asyncpg connection pool.
    """
    global _pool
    if _pool is not None:
        return _pool

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not supabase_url or not service_key:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

    # Convert Supabase URL to PostgreSQL connection string
    # SUPABASE_URL is like: https://project-ref.supabase.co
    # PostgreSQL DSN is: postgresql://postgres:password@db.project-ref.supabase.co:5432/postgres
    dsn = _build_pg_dsn(supabase_url, service_key)

    _pool = await asyncpg.create_pool(dsn=dsn, **_POOL_CONFIG)
    return _pool


def _build_pg_dsn(supabase_url: str, password: str) -> str:
    """Convert a Supabase REST URL to a PostgreSQL PgBouncer pooler DSN.

    Supabase has three connection modes:
      1. Direct connection:   db.{ref}.supabase.co:5432  (NOT for PgBouncer)
      2. Transaction pooler:  {ref}.pooler.supabase.com:6543  ✓ (PgBouncer, recommended)
      3. Session pooler:      {ref}.pooler.supabase.com:5432

    We use the TRANSACTION POOLER (port 6543) with statement_cache_size=0
    to work through Supabase's PgBouncer.

    Handles input formats:
        https://project-ref.supabase.co
        https://project-ref.supabase.co:443

    Returns:
        postgresql://postgres:PASSWORD@project-ref.pooler.supabase.com:6543/postgres
    """
    # Extract project ref from URL
    # https://abcdefghijklm.supabase.co  →  abcdefghijklm
    host = supabase_url.replace("https://", "").replace("http://", "")
    if ":" in host:
        host = host.split(":")[0]

    # Extract project reference (the subdomain before .supabase.co)
    if ".supabase.co" in host:
        project_ref = host.split(".")[0]
        pg_host = f"{project_ref}.pooler.supabase.com"
        pg_port = 6543  # Transaction pooler port
    else:
        # Fallback for custom hosts — direct connection
        pg_host = host
        pg_port = 5432

    return f"postgresql://postgres:{password}@{pg_host}:{pg_port}/postgres"


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


# ── Convenience Helpers ──────────────────────────────────────────


async def async_fetch(query: str, *args) -> list:
    """Fetch multiple rows from the database.

    Args:
        query: SQL query with $1, $2, ... placeholders.
        *args: Values for placeholders.

    Returns:
        List of asyncpg Record objects (dict-like).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def async_fetchrow(query: str, *args) -> Optional[asyncpg.Record]:
    """Fetch a single row from the database.

    Args:
        query: SQL query with $1, $2, ... placeholders.
        *args: Values for placeholders.

    Returns:
        A single asyncpg Record or None if no rows match.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def async_execute(query: str, *args) -> str:
    """Execute a write query (INSERT/UPDATE/DELETE).

    Args:
        query: SQL query with $1, $2, ... placeholders.
        *args: Values for placeholders.

    Returns:
        Command status string (e.g., "INSERT 0 1").
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)


async def async_executemany(query: str, args_list: list) -> None:
    """Execute the same query with multiple parameter sets.

    Args:
        query: SQL query with $1, $2, ... placeholders.
        args_list: List of tuples, each tuple is one set of parameters.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(query, args_list)



