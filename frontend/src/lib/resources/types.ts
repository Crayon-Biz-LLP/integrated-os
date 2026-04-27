export interface Resource {
  id: number;
  url: string | null;
  title: string | null;
  summary: string | null;
  strategic_note: string | null;
  category: string | null;
  mission_id: number | null;
  created_at: string | null;
  enriched_at: string | null;
  mission_title: string | null;
  mission_status: string | null;
  mission_description: string | null;
  hostname: string | null;
}

export interface ResourceMission {
  id: number;
  title: string;
  description: string | null;
  status: string | null;
  resource_count: number;
}

export interface ResourceStats {
  totalResources: number;
  activeMissionsWithResources: number;
  unmappedResources: number;
  recentResources: number;
}

export interface ResourceFilters {
  search?: string;
  mission?: string;
  category?: string;
  sort?: string;
  view?: "mission" | "library";
}

export interface ResourceDetail extends Resource {
  related_resources?: Resource[];
}
