from typing import List, Dict, Any

from dotenv import load_dotenv

load_dotenv()  # Load before importing db

from tests.fixtures.test_tenant import fresh_supabase, resolve_test_tenant_uid  # noqa: E402


class TaskFactory:
    """Create / clean up tasks inside the dedicated TEST TENANT only.

    Every task is created with owner_id = test-tenant uid, and every cleanup
    filters on that same owner_id. A cross-tenant leak is impossible even if
    a [TEST] title prefix ever collided with real data — the owner filter
    physically prevents touching another tenant's rows.
    """

    def __init__(self):
        self.created_task_ids: List[int] = []

    def _owner_id(self) -> str:
        uid = resolve_test_tenant_uid()
        if not uid:
            raise RuntimeError(
                "No test tenant resolvable — set TEST_TENANT_UID or create the "
                "'Test' user. Refusing to create tasks outside a test tenant."
            )
        return uid

    def create_task(self, title: str, status: str = "todo", **kwargs) -> Dict[Any, Any]:
        supabase = fresh_supabase()
        payload = {
            "title": title,
            "status": status,
            "direction": "inbound",
            "is_current": True,
            "owner_id": self._owner_id(),
            **kwargs
        }
        res = supabase.table("tasks").insert(payload).execute()
        if not res.data:
            raise Exception(f"Failed to create task: {payload}")
        task = res.data[0]
        self.created_task_ids.append(task["id"])
        return task

    def teardown(self):
        if not self.created_task_ids:
            return
        supabase = fresh_supabase()
        try:
            supabase.table("tasks").delete().eq("owner_id", self._owner_id()).in_(
                "id", self.created_task_ids
            ).execute()
        except Exception as e:
            print(f"Teardown cleanup error: {e}")

    def cleanup_by_title_prefix(self, prefix: str = "[TEST]"):
        """Delete [TEST]-prefixed tasks owned by the TEST TENANT only.

        The owner_id filter is the leak guard: even if a real task title ever
        matched the prefix, it would not be deleted because it belongs to a
        different owner.
        """
        supabase = fresh_supabase()
        try:
            supabase.table("tasks").delete().eq("owner_id", self._owner_id()).ilike(
                "title", f"{prefix}%"
            ).execute()
        except Exception as e:
            print(f"Prefix cleanup error: {e}")


factory = TaskFactory()
