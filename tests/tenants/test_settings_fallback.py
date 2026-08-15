"""Per-tenant settings fallback (plan §9): a tenant with no user_settings row
gets the defaults; another tenant's settings never leak into their view.
"""

import pytest


import uuid
from unittest.mock import patch

from core.services import user_settings as us
pytestmark = pytest.mark.auth



def _mk(users_rows, settings_rows):
    """Return a fake supabase with canned users / user_settings reads."""
    class Res:
        def __init__(self, data):
            self.data = data

    class Builder:
        def __init__(self, table, rows):
            self._table = table
            self._rows = rows

        def select(self, *a, **k):
            return self

        def eq(self, col, val):
            # filter rows for the requested key
            self._rows = [r for r in self._rows if r.get(col) == val]
            return self

        def limit(self, n):
            return self

        def maybe_single(self):
            return self

        def execute(self):
            return Res(self._rows[0] if self._rows else None)

    class FakeSupabase:
        def __init__(self):
            self._tables = {"users": users_rows, "user_settings": settings_rows}

        def table(self, name):
            return Builder(name, list(self._tables.get(name, [])))

    return FakeSupabase()


def test_unknown_tenant_gets_defaults_no_leak():
    uid_a = str(uuid.uuid4())
    uid_b = str(uuid.uuid4())
    fake = _mk(
        users_rows=[
            {"id": uid_a, "name": "Priya"},
        ],
        settings_rows=[
            {"user_id": uid_a, "timezone": "Asia/Kolkata", "domains": "[]",
             "voice": None, "context": "Priya, COO."},
        ],
    )
    with patch.object(us, "get_supabase", return_value=fake), \
         patch.object(us, "_settings_cache", new={}):
        # Tenant B has no row → defaults (and never A's name/settings).
        base_b = us.load_settings(uid_b)
        assert base_b.name != "Priya", "B inherited A's name"
        assert base_b.timezone == us.DEFAULT_TIMEZONE, "B should fall back to default tz"
        # Tenant A still gets exactly their own row.
        base_a = us.load_settings(uid_a)
        assert base_a.name == "Priya"
        assert "COO" in (base_a.context or "")


def test_settings_cache_is_keyed_by_user_id():
    uid_a, uid_b = str(uuid.uuid4()), str(uuid.uuid4())
    fake = _mk(
        users_rows=[{"id": uid_a, "name": "A"}, {"id": uid_b, "name": "B"}],
        settings_rows=[
            {"user_id": uid_a, "timezone": "Asia/Kolkata", "domains": "[]", "voice": None, "context": "A ctx"},
            {"user_id": uid_b, "timezone": "America/New_York", "domains": "[]", "voice": None, "context": "B ctx"},
        ],
    )
    with patch.object(us, "get_supabase", return_value=fake), \
         patch.object(us, "_settings_cache", new={}):
        a1 = us.load_settings(uid_a)
        b1 = us.load_settings(uid_b)
        a2 = us.load_settings(uid_a)
        assert a1.name == "A" and b1.name == "B" and a2.name == "A"
        assert a1.timezone == "Asia/Kolkata" and b1.timezone == "America/New_York"
