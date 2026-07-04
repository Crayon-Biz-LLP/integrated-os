import { Resource } from "./types";

export async function fetchResource(id: number): Promise<Resource> {
  const res = await fetch(`/api/resources/${id}`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch resource");
  return res.json();
}

export async function fetchRelatedResources(id: number): Promise<Resource[]> {
  const res = await fetch(`/api/resources/${id}/related`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch related resources");
  return res.json();
}

export async function updateResourceCluster(id: number, clusterId: number | null): Promise<void> {
  const res = await fetch(`/api/resources/${id}/cluster`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cluster_id: clusterId }),
  });
  if (!res.ok) throw new Error("Failed to update resource cluster");
}

export async function dismissResource(id: number): Promise<void> {
  const res = await fetch(`/api/resources/${id}/dismiss`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) throw new Error("Failed to dismiss resource");
}
