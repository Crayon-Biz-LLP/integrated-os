"""Regression — audit_logger owner_id key discipline (Tier 4 find).

On the pre-db/78 live schema, audit_logs has NO owner_id column. The
logger previously included `owner_id` (even null) in every insert payload,
which PostgREST rejects (PGRST204 unknown column) — silently killing all
audit logging on the unmigrated DB. Fixed: the key is only included when a
tenant is actually resolved (tenant mode + scoped call).
"""



import os
from unittest.mock import MagicMock, patch

import pytest
pytestmark = pytest.mark.decision


os.environ.setdefault("SUPABASE_URL", "http://localhost:1")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import core.lib.audit_logger as al  # noqa: E402  (env must be set first)


@pytest.fixture(autouse=True)
def _mock_supabase():
    mock_db = MagicMock()
    with patch.object(al, "supabase", mock_db):
        yield mock_db


def test_audit_log_omits_owner_id_when_no_tenant(_mock_supabase):
    """Legacy (pre-db/78, unscoped): no owner_id key in the payload — the
    regression that broke every audit write on the unmigrated live DB."""
    al.audit_log_sync("db", "INFO", "legacy message")
    payload = _mock_supabase.table.return_value.insert.call_args.args[0]
    assert payload["service"] == "db"
    assert payload["level"] == "INFO"
    assert "owner_id" not in payload


def test_audit_log_includes_owner_id_when_tenant_set(_mock_supabase):
    """Tenant mode + scoped call: owner_id stamped for per-tenant dedup
    gates (M4 intent preserved)."""
    with patch("core.services.db.get_tenant", return_value="u1"):
        al.audit_log_sync("db", "INFO", "scoped message")
    payload = _mock_supabase.table.return_value.insert.call_args.args[0]
    assert payload["owner_id"] == "u1"


def test_audit_log_async_omits_owner_id_when_no_tenant(_mock_supabase):
    """The async variant must behave identically."""
    import asyncio

    asyncio.run(al.audit_log("db", "INFO", "legacy async message"))
    payload = _mock_supabase.table.return_value.insert.call_args.args[0]
    assert "owner_id" not in payload
