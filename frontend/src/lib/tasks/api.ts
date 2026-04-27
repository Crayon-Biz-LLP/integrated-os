import { Task, TaskFilters, TaskStats, Project } from "./types";

export async function fetchTasks(filters?: TaskFilters): Promise<Task[]> {
  const params = new URLSearchParams();
  if (filters?.search) params.set("search", filters.search);
  if (filters?.status) params.set("status", filters.status);
  if (filters?.priority) params.set("priority", filters.priority);
  if (filters?.projectId) params.set("projectId", filters.projectId);
  if (filters?.dueWindow) params.set("dueWindow", filters.dueWindow);

  const res = await fetch(`/api/tasks?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch tasks");
  return res.json();
}

export async function fetchTaskStats(): Promise<TaskStats> {
  const res = await fetch(`/api/tasks/stats`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch task stats");
  return res.json();
}

export async function fetchProjects(): Promise<Project[]> {
  const res = await fetch(`/api/tasks/projects`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch projects");
  return res.json();
}

export async function updateTaskProject(taskId: number, projectId: number | null): Promise<void> {
  const res = await fetch(`/api/tasks/${taskId}/project`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_id: projectId }),
  });
  if (!res.ok) throw new Error("Failed to update task project");
}