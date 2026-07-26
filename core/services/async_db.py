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
import json
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
    database password (SUPABASE_DB_PASSWORD) for auth, with
    SUPABASE_SERVICE_ROLE_KEY as fallback for backward compat.

    Returns:
        The asyncpg connection pool.
    """
    global _pool
    if _pool is not None:
        return _pool

    supabase_url = os.getenv("SUPABASE_URL")
    db_password = os.getenv("SUPABASE_DB_PASSWORD") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not supabase_url or not db_password:
        raise ValueError(
            "SUPABASE_URL and SUPABASE_DB_PASSWORD (or SUPABASE_SERVICE_ROLE_KEY) must be set"
        )

    # Convert Supabase URL to PostgreSQL connection string
    dsn = _build_pg_dsn(supabase_url, db_password)

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


def _build_pg_dsn(supabase_url: str, password: str) -> str:
    """Convert a Supabase REST URL to a PostgreSQL Supavisor pooler DSN.

    Supabase connection modes:
      1. Transaction pooler (recommended): aws-{region}.pooler.supabase.com:6543
      2. Session pooler:                   aws-{region}.pooler.supabase.com:5432
      3. Direct connection:                db.{ref}.supabase.co:5432

    We use the TRANSACTION POOLER (port 6543) via Supavisor.
    The pooler hostname requires the AWS region, which varies per project.
    Set SUPABASE_POOLER_HOST env var to override (e.g.,
    "aws-1-ap-southeast-1.pooler.supabase.com").

    Handles input formats:
        https://project-ref.supabase.co
        https://project-ref.supabase.co:443

    The username format for Supavisor is postgres.{project_ref}.

    Returns:
        postgresql://postgres.{ref}:PASSWORD@aws-{region}.pooler.supabase.com:6543/postgres
    """
    # Extract project ref from URL
    # https://abcdefghijklm.supabase.co  →  abcdefghijklm
    host = supabase_url.replace("https://", "").replace("http://", "")
    if ":" in host:
        host = host.split(":")[0]

    if ".supabase.co" in host:
        project_ref = host.split(".")[0]
        # Allow override via env var for region-specific pooler hostname
        pooler_host = os.getenv("SUPABASE_POOLER_HOST")
        if pooler_host:
            pg_host = pooler_host
        else:
            # Fallback: old format (likely won't resolve without region)
            pg_host = f"{project_ref}.pooler.supabase.com"
        pg_port = 6543  # Transaction pooler
        pg_user = f"postgres.{project_ref}"  # Supavisor username format
    else:
        # Fallback for custom hosts — direct connection
        pg_host = host
        pg_port = 5432
        pg_user = "postgres"

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



