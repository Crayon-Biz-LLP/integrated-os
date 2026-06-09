import { Task, Project } from "./types";

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

export async function markTaskDone(taskId: number): Promise<void> {
  const res = await fetch(`/api/tasks/${taskId}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: 'done' }),
  });
  if (!res.ok) throw new Error("Failed to mark task done");
}
