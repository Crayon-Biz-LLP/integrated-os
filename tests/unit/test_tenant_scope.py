"""M1 — tenant scope machinery (plans/69-multi-tenant-product-plan.md).

Unit tests for:
  - tenant context (set / get / scope restore, fail-closed)
  - TenantTable read scoping (owner_id eq pre-applied)
  - write owner injection (insert dict/list, upsert, update)
  - tenant_rpc owner param injection
  - key hashing + resolve_user_by_api_key query shape (incl. pre-db/78)
  - require_api_auth resolution (user key vs legacy key vs unknown/dev)
"""

import hashlib
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "http://localhost:1")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

from core.services import db as db_mod  # noqa: E402  (env must be set first)
from core.services.db import (  # noqa: E402
    hash_api_key,
    require_tenant,
    set_tenant,
    get_tenant,
    tenant_scope,
    tenant_table,
    tenant_rpc,
    TenantRequiredError,
    resolve_user_by_api_key,
)


@pytest.fixture(autouse=True)
def _mock_supabase():
    mock_db = MagicMock()
    with patch.object(db_mod, "get_supabase", return_value=mock_db):
        yield mock_db


@pytest.fixture(autouse=True)
def _no_tenant_leftover():
    """Ensure tenant context is clean between tests."""
    yield
    db_mod._tenant_var.set(None)
    # Also reset the channel-tenant cache to its uncached-probe state: it is
    # a module-level global, so without this the auto-mocked Supabase client
    # would cache a truthy MagicMock and pollute later require_api_auth tests.
    db_mod._channel_tenant = None


# ── Tenant context ────────────────────────────────────────────────────────


def test_tenant_scope_sets_and_restores():
    assert get_tenant() is None
    with tenant_scope("u1"):
        assert get_tenant() == "u1"
        assert require_tenant() == "u1"
    assert get_tenant() is None


def test_require_tenant_fails_closed_without_context():
    with pytest.raises(TenantRequiredError):
        require_tenant()


def test_set_tenant_then_require():
    set_tenant("u2")
    assert require_tenant() == "u2"


# ── TenantTable ───────────────────────────────────────────────────────────


def test_tenant_table_read_is_owner_scoped(_mock_supabase):
    """select() carries the owner filter. The real supabase-py table builder
    has no .eq() until .select() is called, so scoping is applied on the
    select chain (M3 fix) — never at construction."""
    with tenant_scope("u1"):
        tenant_table("tasks").select("*").execute()
    _mock_supabase.table.assert_called_with("tasks")
    _mock_supabase.table.return_value.select.return_value.eq.assert_called_with("owner_id", "u1")


def test_tenant_table_insert_injects_owner(_mock_supabase):
    with tenant_scope("u1"):
        tenant_table("tasks").insert({"title": "ship M1"})
    _mock_supabase.table.return_value.insert.assert_called_with(
        {"title": "ship M1", "owner_id": "u1"}
    )


def test_tenant_table_insert_list_injects_each_preserving_existing(_mock_supabase):
    with tenant_scope("u1"):
        tenant_table("tasks").insert([
            {"title": "a"},
            {"title": "b", "owner_id": "keep"},
        ])
    payload = _mock_supabase.table.return_value.insert.call_args.args[0]
    assert payload == [
        {"title": "a", "owner_id": "u1"},
        {"title": "b", "owner_id": "keep"},
    ]


def test_tenant_table_upsert_injects_and_passes_conflict(_mock_supabase):
    with tenant_scope("u1"):
        tenant_table("core_config").upsert(
            {"key": "season", "content": "{}"}, on_conflict="owner_id,key"
        )
    _mock_supabase.table.return_value.upsert.assert_called_with(
        {"key": "season", "content": "{}", "owner_id": "u1"},
        on_conflict="owner_id,key",
    )


def test_tenant_table_update_injects_owner(_mock_supabase):
    """update() stamps owner_id on the payload AND chains an owner filter so
    a later .eq('id', X) can never touch another tenant's row."""
    with tenant_scope("u1"):
        tenant_table("tasks").update({"status": "done"})
    _mock_supabase.table.return_value.update.assert_called_with(
        {"status": "done", "owner_id": "u1"}
    )
    _mock_supabase.table.return_value.update.return_value.eq.assert_called_with("owner_id", "u1")


def test_tenant_table_delete_is_owner_scoped(_mock_supabase):
    """delete() chains an owner filter so a chained .lt()/.eq() can never
    delete another tenant's rows."""
    with tenant_scope("u1"):
        tenant_table("processed_updates").delete().lt("processed_at", "x")
    _mock_supabase.table.return_value.delete.assert_called_once()
    _mock_supabase.table.return_value.delete.return_value.eq.assert_called_with("owner_id", "u1")


def test_tenant_table_fails_closed_without_context():
    with pytest.raises(TenantRequiredError):
        tenant_table("tasks")


# ── tenant_rpc ────────────────────────────────────────────────────────────


def test_tenant_rpc_injects_owner_param(_mock_supabase):
    with tenant_scope("u1"):
        tenant_rpc("match_memories", {"query_emb": [1.0]})
    _mock_supabase.rpc.assert_called_with(
        "match_memories", {"query_emb": [1.0], "owner_id": "u1"}
    )


def test_tenant_rpc_inject_owner_false_keeps_params(_mock_supabase):
    with tenant_scope("u1"):
        tenant_rpc("admin_sweep", {"a": 1}, inject_owner=False)
    _mock_supabase.rpc.assert_called_with("admin_sweep", {"a": 1})


def test_tenant_rpc_fails_closed_without_context():
    with pytest.raises(TenantRequiredError):
        tenant_rpc("match_memories", {})


# ── API key hashing + resolution ──────────────────────────────────────────


def test_hash_api_key_stable():
    assert hash_api_key("secret") == hashlib.sha256(b"secret").hexdigest()
    assert hash_api_key("secret") != hash_api_key("other")


def test_resolve_user_by_api_key_queries_hash(_mock_supabase):
    _mock_supabase.table.return_value.select.return_value.eq.return_value.limit \
        .return_value.maybe_single.return_value.execute.return_value = MagicMock(
            data={"id": "u1", "name": "Danny", "status": "active"}
        )
    user = resolve_user_by_api_key("my-key")
    assert user["id"] == "u1"
    # the query filters on the sha256 hash, never the raw key
    eq_filter = _mock_supabase.table.return_value.select.return_value.eq.call_args.args
    assert eq_filter == ("api_key_hash", hash_api_key("my-key"))


def test_resolve_user_by_api_key_no_match_returns_none(_mock_supabase):
    _mock_supabase.table.return_value.select.return_value.eq.return_value.limit \
        .return_value.maybe_single.return_value.execute.return_value = MagicMock(data=None)
    assert resolve_user_by_api_key("nope") is None


def test_resolve_user_by_api_key_missing_users_table_returns_none(_mock_supabase):
    # pre-db/78 production: users table doesn't exist → legacy auth fallback
    _mock_supabase.table.side_effect = Exception('relation "users" does not exist')
    assert resolve_user_by_api_key("any") is None


# ── require_api_auth ──────────────────────────────────────────────────────


def _req(api_key: str | None):
    from starlette.requests import Request
    headers = [(b"x-api-key", api_key.encode())] if api_key else []
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": headers,
    })


def test_require_api_auth_sets_tenant_and_returns_uid():
    """M3: require_api_auth is the API-layer tenant carrier. A user key sets
    the tenant for the rest of the handler (FastAPI runs each request in its
    own task, so the contextvar cannot leak between requests; fire-and-forget
    tasks spawned in a handler inherit the caller's tenant, which is correct
    — they process that user's data). Returns the resolved uid so handlers
    could also scope explicitly. No restore happens."""
    import api.index as api
    calls = []
    real_set_tenant = db_mod.set_tenant

    def _recording_set(uid):
        calls.append(uid)
        real_set_tenant(uid)

    with patch.object(api, "resolve_user_by_api_key", return_value={
            "id": "u1", "name": "Danny", "status": "active"}), \
         patch.object(api, "set_tenant", side_effect=_recording_set):
        with tenant_scope("sentinel"):
            # A prior context (e.g. a cron-scoped caller) is REPLACED by the
            # authed user's tenant and kept for the rest of the request.
            uid = api.require_api_auth(_req("user-key"))
            assert uid == "u1"
            assert get_tenant() == "u1"
    # recorded: only the auth set — no restore call
    assert calls == ["u1"]
    # leaving the route's scope restores the clean (None) context
    assert get_tenant() is None


def test_require_api_auth_legacy_key_falls_back_to_channel_tenant():
    """A legacy shared key is authorized, and the tenant falls back to the
    channel tenant (db730b5): the shared key is unscoped by itself, so the
    channel tenant keeps the TenantTable facade satisfied during the
    frontend transition."""
    import api.index as api
    with patch.object(api, "resolve_user_by_api_key", return_value=None), \
         patch.object(db_mod, "resolve_channel_tenant", return_value="u1"), \
         patch.dict(os.environ, {"API_SECRET_KEY": "legacy"}, clear=False):
        uid = api.require_api_auth(_req("legacy"))
    assert uid == "u1"
    assert get_tenant() == "u1"


def test_require_api_auth_legacy_key_stays_unscoped_without_channel_tenant():
    """When no channel tenant resolves (no active user / pre-db/78), a valid
    legacy key still authorizes — but stays unscoped: returns None and sets
    no tenant."""
    import api.index as api
    with patch.object(api, "resolve_user_by_api_key", return_value=None), \
         patch.object(db_mod, "resolve_channel_tenant", return_value=None), \
         patch.dict(os.environ, {"API_SECRET_KEY": "legacy"}, clear=False):
        uid = api.require_api_auth(_req("legacy"))
    assert uid is None
    assert get_tenant() is None


def test_require_api_auth_unknown_key_rejected():
    import api.index as api
    from fastapi import HTTPException
    with patch.object(api, "resolve_user_by_api_key", return_value=None), \
         patch.dict(os.environ, {"API_SECRET_KEY": "legacy"}, clear=False):
        with pytest.raises(HTTPException) as exc:
            api.require_api_auth(_req("bad-key"))
    assert exc.value.status_code == 401
    assert get_tenant() is None  # failed auth leaves no stale tenant


def test_require_api_auth_fails_closed_without_env():
    """Audit 2.2: missing API_SECRET_KEY must NOT leave the API open.
    Without the explicit ALLOW_DEV_AUTH=1 dev flag, an unauthenticated
    request is rejected."""
    import api.index as api
    from fastapi import HTTPException
    with patch.dict(os.environ, {}, clear=False):
        if "API_SECRET_KEY" in os.environ:
            del os.environ["API_SECRET_KEY"]
        if "ALLOW_DEV_AUTH" in os.environ:
            del os.environ["ALLOW_DEV_AUTH"]
        with pytest.raises(HTTPException) as exc:
            api.require_api_auth(_req(None))  # no key, no env → rejected
    assert exc.value.status_code == 503
    assert get_tenant() is None


def test_require_api_auth_dev_mode_allows_with_flag():
    """Dev mode is explicit: ALLOW_DEV_AUTH=1 opts into open auth locally,
    falling back to the channel tenant when one resolves (db730b5)."""
    import api.index as api
    with patch.dict(os.environ, {"ALLOW_DEV_AUTH": "1"}, clear=False):
        if "API_SECRET_KEY" in os.environ:
            del os.environ["API_SECRET_KEY"]
        with patch.object(db_mod, "resolve_channel_tenant", return_value="u1"):
            uid = api.require_api_auth(_req(None))  # dev flag set → allowed
    assert uid == "u1"
    assert get_tenant() == "u1"


# ── Context isolation across concurrent async work ─────────────────────────


def test_tenant_context_isolated_across_concurrent_async_tasks():
    """Two concurrent coroutines each carry their own tenant — proving the
    contextvar cannot bleed between requests sharing an event loop (the
    home-feed gather pattern, background tasks, etc.)."""
    import asyncio

    async def worker(uid: str, seen: dict):
        with tenant_scope(uid):
            await asyncio.sleep(0.01)
            seen[uid] = get_tenant()

    async def run():
        seen = {}
        await asyncio.gather(
            worker("uA", seen),
            worker("uB", seen),
            worker("uA", seen),
        )
        return seen

    seen = asyncio.run(run())
    assert seen == {"uA": "uA", "uB": "uB"}


# ── tenant_mode_enabled legacy detection ─────────────────────────────────


def _reset_tenant_mode():
    db_mod._tenant_mode = None


def test_tenant_mode_enabled_confirmed_false_on_postgrest_pgrst204(_mock_supabase):
    """Regression (found by Tier 4 on live): PostgREST answers a missing
    table with PGRST204 ('Could not find the 'users' relation in the schema
    cache') — NOT the native 'relation ... does not exist' shape. The probe
    must recognize it AND cache the confirmed False so it doesn't re-probe
    on every call (per-call probe = failing HTTP round trip + log spam)."""
    _reset_tenant_mode()
    _mock_supabase.table.side_effect = Exception(
        "Could not find the 'users' relation in the schema cache"
    )
    assert db_mod.tenant_mode_enabled() is False
    assert db_mod._tenant_mode is False  # confirmed → cached
    calls = _mock_supabase.table.call_count
    db_mod.tenant_mode_enabled()
    assert _mock_supabase.table.call_count == calls  # no re-probe


def test_tenant_mode_enabled_confirmed_false_native_shape(_mock_supabase):
    _reset_tenant_mode()
    _mock_supabase.table.side_effect = Exception('relation "users" does not exist')
    assert db_mod.tenant_mode_enabled() is False
    assert db_mod._tenant_mode is False


def test_tenant_mode_enabled_transient_failure_not_cached(_mock_supabase):
    """A network/auth blip carries no 'table missing' signal: return False
    WITHOUT caching so scoping retries next call — never silently disable
    tenant mode for the process lifetime (that would be the worst leak)."""
    _reset_tenant_mode()
    _mock_supabase.table.side_effect = Exception("connection timed out")
    assert db_mod.tenant_mode_enabled() is False
    assert db_mod._tenant_mode is None  # not confirmed → re-probe next call


def test_tenant_mode_enabled_true_cached(_mock_supabase):
    _reset_tenant_mode()
    res = MagicMock(data=[{"id": "u1"}])
    _mock_supabase.table.return_value.select.return_value.limit.return_value \
        .execute.return_value = res
    assert db_mod.tenant_mode_enabled() is True
    assert db_mod._tenant_mode is True
    calls = _mock_supabase.table.call_count
    db_mod.tenant_mode_enabled()
    assert _mock_supabase.table.call_count == calls


def test_tenant_scope_restores_outer_context_on_exception():
    """tenant_scope restores the previous tenant even when the block raises —
    no orphaned token leaves a stale tenant for later work."""
    set_tenant("outer")
    try:
        with pytest.raises(ValueError):
            with tenant_scope("inner"):
                assert get_tenant() == "inner"
                raise ValueError("boom")
        assert get_tenant() == "outer"
    finally:
        db_mod._tenant_var.set(None)
