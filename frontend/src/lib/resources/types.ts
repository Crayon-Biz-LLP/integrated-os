export interface Resource {
  id: number;
  url: string | null;
  title: string | null;
  summary: string | null;
  strategic_note: string | null;
  category: string | null;
  cluster_id: number | null;
  created_at: string | null;
  enriched_at: string | null;
  cluster_title: string | null;
  cluster_status: string | null;
  cluster_description: string | null;
  hostname: string | null;
}

export interface ResourceCluster {
  id: number;
  title: string;
  description: string | null;
  status: string | null;
  resource_count: number;
}

export interface ResourceStats {
  totalResources: number;
  activeClustersWithResources: number;
  unmappedResources: number;
  recentResources: number;
}

export interface ResourceFilters {
  search?: string;
  cluster?: string;
  category?: string;
  sort?: string;
  view?: "cluster" | "library";
}

export interface ResourceDetail extends Resource {
  related_resources?: Resource[];
}
