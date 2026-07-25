import { Task } from "./types";

export async function fetchOrganizations(): Promise<{ id: string; name: string }[]> {
  const res = await fetch(`/api/organizations`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch organizations");
  return res.json();
}

export async function markTaskDone(taskId: number): Promise<void> {
  const res = await fetch(`/api/tasks/${taskId}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: 'done' }),
  });
  if (!res.ok) throw new Error("Failed to mark task done");
}
