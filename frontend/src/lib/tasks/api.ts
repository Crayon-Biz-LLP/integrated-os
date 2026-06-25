import { Task, Project } from "./types";

export async function fetchProjects(): Promise<Project[]> {
  const res = await fetch(`/api/tasks/projects`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch projects");
  return res.json();
}

export async function fetchOrganizations(): Promise<{ id: string; name: string }[]> {
  const res = await fetch(`/api/organizations`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch organizations");
  return res.json();
}

export async function updateTaskProject(taskId: number, projectId: number | null, organizationId?: string | null): Promise<void> {
  const body: any = { project_id: projectId };
  if (organizationId !== undefined) {
    body.organization_id = organizationId;
  }
  
  const res = await fetch(`/api/tasks/${taskId}/project`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
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
