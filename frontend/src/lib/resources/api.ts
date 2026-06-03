import { Resource, ResourceFilters, ResourceStats, ResourceCluster, ResourceDetail } from "./types";

export async function fetchResources(filters?: ResourceFilters): Promise<Resource[]> {
  const params = new URLSearchParams();
  if (filters?.search) params.set("search", filters.search);
  if (filters?.cluster) params.set("cluster", filters.cluster);
  if (filters?.category) params.set("category", filters.category);
  if (filters?.sort) params.set("sort", filters.sort);
  if (filters?.view) params.set("view", filters.view);

  const res = await fetch(`/api/resources?${params.toString()}`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch resources");
  return res.json();
}

export async function fetchResourceStats(): Promise<ResourceStats> {
  const res = await fetch(`/api/resources/stats`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch resource stats");
  return res.json();
}

export async function fetchResourceClusters(): Promise<ResourceCluster[]> {
  const res = await fetch(`/api/resources/clusters`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch clusters");
  return res.json();
}

export async function fetchResource(id: number): Promise<ResourceDetail> {
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
