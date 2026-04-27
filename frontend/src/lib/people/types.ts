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
  project_id: number | null;
  project_name: string | null;
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
