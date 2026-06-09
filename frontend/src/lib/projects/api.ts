import { Project, ProjectTask } from "./types";

export async function updateProjectStatus(id: number, status: string): Promise<Project> {
  const res = await fetch(`/api/projects/${id}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!res.ok) throw new Error("Failed to update project status");
  return res.json();
}

export async function fetchProjectTasks(projectId: number): Promise<ProjectTask[]> {
  const res = await fetch(`/api/projects/${projectId}/tasks`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch project tasks");
  return res.json();
}
