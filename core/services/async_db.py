"""
asyncpg removed — PostgREST is faster on Modal (plan 68 revert evidence).

async_fetch / async_fetchrow are no-ops that return empty results.
Callers (classify.py, dispatch.py, handler.py) already handle None/[]
and fall back to PostgREST.
"""

from typing import Optional

# asyncpg pool removed — PostgREST is faster on Modal (plan 68).
# async_fetch/async_fetchrow below are no-ops; callers fall back to PostgREST.


async def async_fetch(query: str, *args) -> list:
    """No-op: asyncpg removed (PostgREST is faster on Modal). Callers fall back to PostgREST."""
    return []


async def async_fetchrow(query: str, *args) -> Optional[dict]:
    """No-op: asyncpg removed (PostgREST is faster on Modal). Callers fall back to PostgREST."""
    return None


# Keep these as no-ops for import compatibility
async def async_select(*args, **kwargs) -> list:
    return []

async def async_select_one(*args, **kwargs) -> Optional[dict]:
    return None

async def async_execute(*args, **kwargs) -> str:
    return ""

async def async_executemany(*args, **kwargs) -> None:
    pass
