import { Project, ProjectFilters, ProjectStats } from "./types";

export async function fetchProjects(filters?: ProjectFilters): Promise<Project[]> {
  const params = new URLSearchParams();
  if (filters?.search) params.set("search", filters.search);
  if (filters?.orgTag) params.set("orgTag", filters.orgTag);
  if (filters?.context) params.set("context", filters.context);
  if (filters?.status) params.set("status", filters.status);

  const res = await fetch(`/api/projects?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch projects");
  return res.json();
}

export async function fetchProjectStats(): Promise<ProjectStats> {
  const res = await fetch(`/api/projects/stats`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch project stats");
  return res.json();
}

export async function updateProjectStatus(id: number, status: string): Promise<Project> {
  const res = await fetch(`/api/projects/${id}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!res.ok) throw new Error("Failed to update project status");
  return res.json();
}