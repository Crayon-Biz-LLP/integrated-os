from typing import List, Dict, Any

from dotenv import load_dotenv

load_dotenv()  # Load before importing db

from core.services.db import get_supabase  # noqa: E402


class TaskFactory:
    def __init__(self):
        self.created_task_ids: List[int] = []

    def create_task(self, title: str, status: str = "todo", **kwargs) -> Dict[Any, Any]:
        supabase = get_supabase()
        payload = {
            "title": title,
            "status": status,
            "direction": "inbound",
            "is_current": True,
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
        supabase = get_supabase()
        try:
            supabase.table("tasks").delete().in_("id", self.created_task_ids).execute()
        except Exception as e:
            print(f"Teardown cleanup error: {e}")

    def cleanup_by_title_prefix(self, prefix: str = "[TEST]"):
        supabase = get_supabase()
        try:
            supabase.table("tasks").delete().ilike("title", f"{prefix}%").execute()
        except Exception as e:
            print(f"Prefix cleanup error: {e}")


factory = TaskFactory()
