"""Shared fixtures for the tests/tenants isolation suite.

`tenants` fixture (module-scoped): creates two active test users (A and B)
plus marker rows on every key tenant table, yields their uids, and deletes
everything (users + markers) on teardown. Skips when the copy DB is
unreachable.
"""

import pytest

from tests.tenants.db_utils import MARK, db_available, psql

_UID_A = "00000000-0000-0000-0000-0000000000a1"
_UID_B = "00000000-0000-0000-0000-0000000000b1"

_TABLES = {
    "tasks": (
        f"insert into tasks (title, status, is_current, direction, owner_id) "
        f"values ('{MARK}A-TASK', 'active', true, 'inbound', '{_UID_A}')",
        f"insert into tasks (title, status, is_current, direction, owner_id) "
        f"values ('{MARK}B-TASK', 'active', true, 'inbound', '{_UID_B}')",
        "delete from tasks where owner_id in ('%s','%s') and title like '%s%%'" % (_UID_A, _UID_B, MARK),
    ),
    "graph_nodes": (
        f"insert into graph_nodes (label, type, is_current, normalized_label, metadata, owner_id) "
        f"values ('{MARK}ANode', 'concept', true, '{MARK}anode', '{{}}'::jsonb, '{_UID_A}')",
        f"insert into graph_nodes (label, type, is_current, normalized_label, metadata, owner_id) "
        f"values ('{MARK}BNode', 'concept', true, '{MARK}bnode', '{{}}'::jsonb, '{_UID_B}')",
        "delete from graph_nodes where owner_id in ('%s','%s') and label like '%s%%'" % (_UID_A, _UID_B, MARK),
    ),
    "core_config": (
        f"insert into core_config (key, content, owner_id) values ('{MARK}a', 'secret-a', '{_UID_A}') "
        f"on conflict (owner_id, key) do nothing",
        f"insert into core_config (key, content, owner_id) values ('{MARK}b', 'secret-b', '{_UID_B}') "
        f"on conflict (owner_id, key) do nothing",
        "delete from core_config where owner_id in ('%s','%s') and key like '%s%%'" % (_UID_A, _UID_B, MARK),
    ),
    "device_tokens": (
        f"insert into device_tokens (token, platform, created_at, updated_at, owner_id) "
        f"values ('{MARK}a-token', 'android', now(), now(), '{_UID_A}')",
        f"insert into device_tokens (token, platform, created_at, updated_at, owner_id) "
        f"values ('{MARK}b-token', 'android', now(), now(), '{_UID_B}')",
        "delete from device_tokens where owner_id in ('%s','%s') and token like '%s%%'" % (_UID_A, _UID_B, MARK),
    ),
    "memories": (
        f"insert into memories (content, owner_id) values ('{MARK}a-memory', '{_UID_A}')",
        f"insert into memories (content, owner_id) values ('{MARK}b-memory', '{_UID_B}')",
        "delete from memories where owner_id in ('%s','%s') and content like '%s%%'" % (_UID_A, _UID_B, MARK),
    ),
}


@pytest.fixture(scope="module")
def tenants():
    if not db_available():
        pytest.skip(f"copy DB unreachable — set TENANTS_DSN (tried {__import__('tests.tenants.db_utils', fromlist=['DSN']).DSN})")
    for uid, name in ((_UID_A, "TenantTestA"), (_UID_B, "TenantTestB")):
        psql(
            f"insert into users (id, name, status, api_key_hash) "
            f"values ('{uid}', '{name}', 'active', '{MARK}{name.lower()}-hash') "
            f"on conflict (id) do nothing"
        )
    for a_ins, b_ins, _cleanup in _TABLES.values():
        psql(a_ins)
        psql(b_ins)
    yield {"a": _UID_A, "b": _UID_B}
    # teardown — delete markers first, then the users
    for _a, _b, cleanup in _TABLES.values():
        try:
            psql(cleanup)
        except Exception:
            pass
    for uid in (_UID_A, _UID_B):
        psql(f"delete from users where id = '{uid}'")
