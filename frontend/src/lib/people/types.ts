export interface Person {
  id: number;
  name: string;
  role: string | null;
  strategic_weight: number | null;
  created_at: string | null;
  active_task_count: number;
}

export interface PersonTask {
  id: number;
  title: string;
  status: string;
  priority: string;
  reminder_at: string | null;
  deadline: string | null;
  created_at: string | null;
  organization_id: string | null;
  organization_name: string | null;
}

export interface PeopleStats {
  total: number;
  highPriority: number;
  withActiveTasks: number;
  recentlyAdded: number;
}

export interface PeopleFilters {
  search?: string;
  tier?: string;
  sort?: string;
}

export interface PersonAlias {
  id: number;
  alias: string;
  canonical_name: string;
  resolution_count: number;
  last_resolved_at: string | null;
  created_at: string | null;
}
