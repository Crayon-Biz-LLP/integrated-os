import asyncio
import contextvars
import hashlib
import os
from contextlib import contextmanager
from supabase import create_client, Client

_supabase: Client = None


def get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        _supabase = create_client(
            os.getenv("SUPABASE_URL"),
            os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        )
    return _supabase


async def exec_query(builder):
    """Execute a Supabase query builder off the event loop.

    The supabase-py client is SYNCHRONOUS: a bare .execute() blocks the
    calling thread. In async handlers that stalls the entire event loop,
    turning asyncio.gather into serial execution and queueing every
    concurrent request behind the current one (the 20s screen-load
    symptom). Offloading to a worker thread lets parallel queries actually
    run in parallel and keeps the loop free for other requests.

    Usage: res = await exec_query(supabase.table("t").select("*").eq(...))
    """
    return await asyncio.to_thread(builder.execute)



def maybe_single_safe(builder):
    """Execute a builder chain with .limit(1).maybe_single() guard.

    Prevents the silent-null-on-multi-match failure mode of bare
    maybe_single(). Always caps the result set to 1 row before
    singularizing, so multiple matching rows return the first match
    instead of silently returning None.

    Usage:
        result = maybe_single_safe(
            supabase.table('people').select('id, name').eq('id', person_id)
        )
        if result.data:
            name = result.data['name']

    Args:
        builder: A Supabase query builder chain (e.g., from .table().select().eq()...)

    Returns:
        Same shape as builder.execute() — an object with .data attribute.
        .data is the row dict (exactly 1 match) or None (0 matches).
        Multiple matches are silently capped to the first row — consider
        adding explicit ordering if the first-match bias is wrong for
        your use case.
    """
    return builder.limit(1).maybe_single().execute()


def query_list_safe(builder, max_results=100):
    """Execute a query builder with an upper bound on results.

    Prevents unbounded result sets from queries that don't specify
    an explicit .limit(). Adds a cap if none is set by the caller.

    Usage:
        items = query_list_safe(
            supabase.table('tasks').select('id, title').eq('status', 'active'),
            max_results=50
        )
        for item in items.data or []:
            ...

    Args:
        builder: A Supabase query builder chain.
        max_results: Maximum number of rows to return (default 100).

    Returns:
        Same shape as builder.execute().
    """
    return builder.limit(max_results).execute()


def zombie_recovery():
    from datetime import datetime, timezone, timedelta
    # M3: tenant facade — sentinel runs this per-tenant (M4 fan-out); without
    # scoping, tenant A's sentinel would reset tenant B's stuck dumps.
    # Legacy pre-db/78 (no tenant mode) runs unscoped exactly as before.
    supabase = tenant_aware_client()
    from core.lib.audit_logger import audit_log_sync
    try:
        ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        
        # Recover dumps stuck in 'processing' — atomic status claim via in_ filter
        proc_res = supabase.table('raw_dumps') \
            .update({"status": "staged"}) \
            .eq('status', 'processing') \
            .lt('created_at', ten_mins_ago) \
            .execute()
        proc_count = len(proc_res.data or [])
        if proc_count:
            audit_log_sync("db", "INFO", f"Zombie recovery: reset {proc_count} 'processing' dumps to 'staged'")
        
        # Recover orphaned completion dumps stuck in processing_completion
        comp_res = supabase.table('raw_dumps') \
            .update({"status": "awaiting_completion_match"}) \
            .eq('status', 'processing_completion') \
            .lt('created_at', ten_mins_ago) \
            .execute()
        comp_count = len(comp_res.data or [])
        if comp_count:
            audit_log_sync("db", "INFO", f"Zombie recovery: reset {comp_count} 'processing_completion' dumps")
            
    except Exception as e:
        audit_log_sync("db", "WARNING", f"Zombie recovery failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# M1 — Tenant context & scoped data layer (plans/69-multi-tenant-product-plan)
#
# The legacy helpers above (get_supabase / exec_query / maybe_single_safe)
# remain unscoped — they are migrated onto the tenant layer during the M3
# sweep. New code should use tenant_table() / tenant_rpc() inside a tenant
# context (tenant_scope()). NOTE: require_api_auth only carries the tenant
# DURING the auth call (it restores the prior context before returning), so
# handlers must establish their own scope — wrap the handler body in
# `with tenant_scope(user_id):` before using tenant_table()/tenant_rpc().
# ═══════════════════════════════════════════════════════════════════════════

_tenant_var: contextvars.ContextVar = contextvars.ContextVar("tenant_id", default=None)


class TenantRequiredError(RuntimeError):
    """Raised when tenant-scoped access is attempted outside a tenant context."""


def set_tenant(user_id: str) -> None:
    """Set the tenant (user id) for the current execution context."""
    _tenant_var.set(user_id)


def get_tenant() -> str | None:
    """Return the current tenant user id, or None when unscoped."""
    return _tenant_var.get()


def require_tenant() -> str:
    """Return the current tenant user id or fail closed."""
    uid = _tenant_var.get()
    if not uid:
        raise TenantRequiredError(
            "tenant context required — set it via require_api_auth() or tenant_scope()"
        )
    return uid


class tenant_scope:
    """Context manager: run a block under a tenant (restores on exit)."""

    def __init__(self, user_id: str):
        self._uid = user_id
        self._token = None

    def __enter__(self) -> "tenant_scope":
        self._token = _tenant_var.set(self._uid)
        return self

    def __exit__(self, *exc) -> bool:
        _tenant_var.reset(self._token)
        return False


def _inject_owner(data, uid: str):
    """Add owner_id to insert/update payloads (dict or list of dicts)."""
    if isinstance(data, dict):
        out = dict(data)
        out.setdefault("owner_id", uid)
        return out
    return [dict(r, owner_id=uid) if not r.get("owner_id") else dict(r) for r in data]


# Tables whose tenant key IS the row key (they have no owner_id column).
# The facade passes these through UN-scoped — callers always filter on
# user_id/id explicitly, so there is no cross-tenant read/write surface.
# (M8 onboarding: user_settings / user_oauth_tokens / users.)
_TENANT_KEYED_TABLES = frozenset({"users", "user_settings", "user_oauth_tokens"})


class TenantTable:
    """Tenant-scoped PostgREST builder for one table.

    Reads:  .select() is intercepted and pre-filtered with
            .eq('owner_id', tenant) — every chained eq/order/limit/execute
            inherits the scope. For _TENANT_KEYED_TABLES (users,
            user_settings, user_oauth_tokens — no owner_id column) the chain
            is passed through raw; the caller filters on user_id/id.
    Writes: insert / upsert / update inject owner_id into the payload AND
            owner-scope the WHERE clause (update and delete append
            .eq('owner_id', tenant) so a chained filter can never touch
            another tenant's row). Same tenant-keyed passthrough for the
            three no-owner tables.

    Fail-closed: constructing outside a tenant context raises
    TenantRequiredError.
    """

    def __init__(self, name: str):
        self._uid = require_tenant()
        self._name = name
        # NOTE: do NOT chain .eq() here — the real supabase-py table builder
        # has no filters until .select() is called. Scoping is applied in
        # select()/delete()/update() below.
        self._inner = get_supabase().table(name)
        self._keyed = name in _TENANT_KEYED_TABLES

    def select(self, columns="*", **kwargs):
        # Intercept the read chain: every select is owner-scoped (except the
        # tenant-keyed tables, which carry no owner_id column).
        chain = self._inner.select(columns, **kwargs)
        if not self._keyed:
            chain = chain.eq("owner_id", self._uid)
        return chain

    def __getattr__(self, item):
        # Other read-side verbs (eq/order/limit/execute/...) delegate to the
        # raw table builder — callers only reach them AFTER select() (which
        # already applied the owner filter).
        return getattr(self._inner, item)

    def insert(self, data):
        if self._keyed:
            return self._inner.insert(data)
        return get_supabase().table(self._name).insert(_inject_owner(data, self._uid))

    def upsert(self, data, on_conflict=None, **kwargs):
        if self._keyed:
            return self._inner.upsert(data, on_conflict=on_conflict, **kwargs)
        return get_supabase().table(self._name).upsert(
            _inject_owner(data, self._uid), on_conflict=on_conflict, **kwargs
        )

    def update(self, data):
        if self._keyed:
            return self._inner.update(data)
        return (
            get_supabase()
            .table(self._name)
            .update(_inject_owner(data, self._uid))
            .eq("owner_id", self._uid)
        )

    def delete(self):
        if self._keyed:
            return self._inner.delete()
        return self._inner.delete().eq("owner_id", self._uid)


def tenant_table(name: str) -> TenantTable:
    """Return a tenant-scoped table builder (fail-closed without a tenant)."""
    return TenantTable(name)


# ── M3: tenant-mode detection & channel-tenant resolution ──────────────────

_tenant_mode: bool | None = None


def _missing_table_error(msg: str, table: str = "users") -> bool:
    """True when an error provably means `table` is missing — both the
    native shape ('relation "users" does not exist') and PostgREST's
    PGRST204 ('Could not find the 'users' relation in the schema cache').
    Anything else (timeout, auth, rate limit) is a transient failure and
    must NOT be treated as confirmed-missing."""
    return table in msg and (
        "does not exist" in msg or "not found" in msg or "schema cache" in msg
    )


def tenant_mode_enabled() -> bool:
    """True once the users table exists (db/78 applied) — the scoped world.

    Cached only on CONFIRMED outcomes: True when the probe succeeds, and
    False only when the error provably means the users table is missing
    (pre-db/78). A transient probe failure (network blip, rate limit, auth)
    returns False WITHOUT caching — so scoping retries on the next call
    instead of being silently disabled for the process lifetime (the
    worst-case leak: tenant mode off == legacy unscoped access, no error).
    """
    global _tenant_mode
    if _tenant_mode is not None:
        return _tenant_mode
    try:
        get_supabase().table("users").select("id").limit(1).execute()
        _tenant_mode = True
    except Exception as e:
        msg = str(e)
        if _missing_table_error(msg):
            _tenant_mode = False  # confirmed: pre-db/78
        else:
            try:
                from core.lib.audit_logger import audit_log_sync
                audit_log_sync("db", "WARNING",
                    f"tenant_mode probe failed (scoping retried next call): {type(e).__name__}")
            except Exception:
                pass
    return bool(_tenant_mode)


_channel_tenant: str | None = None
# Sentinel: cached once we've CONFIRMED there is no resolvable channel
# tenant (users table missing, or no active user) — distinct from None
# (uncached probe state) so transient failures re-probe.
_NO_CHANNEL_TENANT = object()


def resolve_channel_tenant() -> str | None:
    """The tenant user id for channel-originated traffic (Telegram webhooks,
    cron) which carries no API key.

    Today there is exactly one active user (tenant #1, Danny) — return it.
    When users exist but none is active, return None (legacy unscoped). When
    the users table is missing (pre-db/78), return None. Cached per process;
    only CONFIRMED outcomes are cached (a confirmed miss is cached, a
    transient failure re-probes).
    """
    global _channel_tenant
    if _channel_tenant is _NO_CHANNEL_TENANT:
        return None
    if _channel_tenant is not None:
        return _channel_tenant
    try:
        res = (
            get_supabase()
            .table("users")
            .select("id")
            .eq("status", "active")
            .order("created_at")
            .limit(1)
            .maybe_single()
            .execute()
        )
        if res.data:
            _channel_tenant = res.data["id"]
            return _channel_tenant
        # Confirmed: users table exists but no active user (or empty).
        _channel_tenant = _NO_CHANNEL_TENANT
    except Exception as e:
        msg = str(e)
        if _missing_table_error(msg):
            _channel_tenant = _NO_CHANNEL_TENANT  # confirmed: pre-db/78
    return None


class TenantAwareClient:
    """Facade over the Supabase client: routes every table/rpc call through
    the tenant layer when tenant mode is active, else legacy unscoped.

    This is the M3 sweep mechanism for module-level `supabase` bindings: a
    module imports one object and ALL its call sites become tenant-scoped
    without a 155-site mechanical rewrite. Fail-closed: when tenant mode is
    on but no tenant context is set, tenant_table()/tenant_rpc() raise
    TenantRequiredError — a forgotten scope is a loud error, not a leak.
    """

    def table(self, name):
        if tenant_mode_enabled():
            return tenant_table(name)
        return get_supabase().table(name)

    def rpc(self, name, params=None):
        if tenant_mode_enabled():
            if name in _GLOBAL_RPCS:
                # Explicitly global (sequence/admin) — no owner injection.
                return get_supabase().rpc(name, params or {})
            return tenant_rpc(name, params)
        return get_supabase().rpc(name, params or {})

    def __getattr__(self, item):
        # Any other client attribute (auth, storage, ...) → legacy client.
        return getattr(get_supabase(), item)


def tenant_aware_client() -> TenantAwareClient:
    """Return the shared tenant-aware client facade (M3 sweep binding)."""
    return TenantAwareClient()


@contextmanager
def channel_tenant_scope():
    """Run a block under the channel tenant — Telegram webhooks and cron
    traffic carry no API key, so the tenant comes from
    resolve_channel_tenant().

    No-op when a tenant context is already active (nested calls, API-driven
    paths that set their own scope). Pre-db/78 (no users table) resolves to
    None → runs unscoped legacy, exactly as before.
    """
    if get_tenant():
        yield
        return
    uid = resolve_channel_tenant()
    if uid:
        with tenant_scope(uid):
            yield
    else:
        yield


def tenant_rpc(name: str, params: dict | None = None, inject_owner: bool = True):
    """Call an RPC with owner_id injected (fail-closed without a tenant).

    RPC signatures gain an owner_id param during the M3 sweep; set
    inject_owner=False for admin RPCs that must stay global.
    """
    uid = require_tenant()
    payload = dict(params or {})
    if inject_owner:
        owner_param = _RPC_OWNER_PARAM.get(name, "owner_id")
        payload.setdefault(owner_param, uid)
    return get_supabase().rpc(name, payload)


# RPCs that operate on NO tenant data and must stay global: pure sequence
# helpers (next_clarification_shortcode), admin/SQL pass-through (run_sql).
# Everything else is assumed tenant-data-scoped: the facade fails closed
# (injects owner_id — the Postgres signature gains the param in db/80) so a
# forgotten scope is a loud error, not a silent cross-tenant read.
_GLOBAL_RPCS = {"next_clarification_shortcode", "run_sql"}

# RPCs whose owner param is NOT literally `owner_id`. These functions write
# rows into tables that have an owner_id column, so a param named owner_id
# would be ambiguous in PL/pgSQL (PG17 hard-error on unqualified reference);
# the param is named p_owner instead and the facade injects under that name.
_RPC_OWNER_PARAM: dict[str, str] = {
    "archive_terminal_pending_edges": "p_owner",
    "batch_whatsapp_message": "p_owner",
}


def hash_api_key(api_key: str) -> str:
    """SHA-256 hex of a per-user API key (never store the key itself)."""
    return hashlib.sha256(api_key.encode()).hexdigest()


# ── M4: cron fan-out — one cron run serves all active users ──────────────────

def active_user_ids() -> list[str]:
    """All active user ids (users.status = 'active'), oldest first.

    The cron fan-out primitive (M4): sentinel / decision-pulse / roundup
    iterate this list, running per-tenant under tenant_scope(). Returns []
    when the users table is missing (pre-db/78) or no user is active —
    callers then run once unscoped (exact legacy behaviour).
    """
    try:
        res = (
            get_supabase()
            .table("users")
            .select("id")
            .eq("status", "active")
            .order("created_at")
            .execute()
        )
        return [r["id"] for r in (res.data or [])]
    except Exception:
        return []


def core_config_upsert(supabase, row: dict):
    """Upsert a core_config row with the conflict target matching tenant mode.

    db/78 changed core_config uniqueness from (key) to (owner_id, key), so
    PostgREST's on_conflict must name the actual unique constraint: the
    tenant facade injects owner_id into the payload, making 'owner_id,key'
    correct in tenant mode; legacy unscoped mode (pre-db/78, no owner_id
    column) keeps 'key'. Every core_config upsert must go through here — a
    bare on_conflict='key' 400s once db/78 lands.
    """
    if tenant_mode_enabled():
        return supabase.table("core_config").upsert(row, on_conflict="owner_id,key")
    return supabase.table("core_config").upsert(row, on_conflict="key")


def resolve_telegram_chat_id(user_id: str | None = None) -> str | None:
    """Per-tenant Telegram chat id: users.telegram_chat_id → env (legacy).

    Returns None for app-only tenants (no chat configured) so callers skip
    Telegram sends gracefully (the Android app is the primary channel).
    Resolution:
      1. users.telegram_chat_id for the given/current tenant (db/83).
      2. env TELEGRAM_CHAT_ID when exactly one active user exists — the
         legacy single-user world (Danny) where the env var is that user's
         chat. Once a second user is added, un-configured users get None
         instead of inheriting someone else's chat (no cross-tenant
         Telegram leak).
      3. env TELEGRAM_CHAT_ID when no tenant context is set (legacy
         unscoped / pre-db/78 code paths).
    """
    uid = user_id or get_tenant()
    if uid:
        try:
            res = (
                get_supabase()
                .table("users")
                .select("telegram_chat_id")
                .eq("id", uid)
                .limit(1)
                .maybe_single()
                .execute()
            )
            if res.data and res.data.get("telegram_chat_id"):
                return str(res.data["telegram_chat_id"]).strip() or None
        except Exception:
            pass  # pre-db/83: column missing → env fallback below
        # No per-user chat id: inherit env ONLY while this is still the
        # single-user world — otherwise the env chat belongs to someone else.
        try:
            if len(active_user_ids()) <= 1:
                raw = os.getenv("TELEGRAM_CHAT_ID")
                return raw.strip() if raw else None
        except Exception:
            return None
        return None
    raw = os.getenv("TELEGRAM_CHAT_ID")
    return raw.strip() if raw else None


_missing_users_logged = False


def resolve_user_by_api_key(api_key: str) -> dict | None:
    """Resolve X-API-Key → user row (users.api_key_hash, sha256).

    Returns None when the key matches no user — including when the users
    table does not exist yet (pre-db/78 production), so callers fall back
    to the legacy shared-key path.
    """
    global _missing_users_logged
    try:
        res = (
            get_supabase()
            .table("users")
            .select("id, name, status")
            .eq("api_key_hash", hash_api_key(api_key))
            .limit(1)
            .maybe_single()
            .execute()
        )
        return res.data if res.data else None
    except Exception as e:
        msg = str(e)
        if _missing_table_error(msg):
            if not _missing_users_logged:
                try:
                    from core.lib.audit_logger import audit_log_sync
                    audit_log_sync("db", "INFO", "users table missing — legacy shared-key auth only (pre-db/78)")
                except Exception:
                    pass
                _missing_users_logged = True
        else:
            try:
                from core.lib.audit_logger import audit_log_sync
                audit_log_sync("db", "WARNING", f"resolve_user_by_api_key failed: {type(e).__name__}: {e}")
            except Exception:
                pass
        return None


