export interface Task {
  id: number;
  title: string;
  status: string;
  priority: string;
  project_id: number | null;
  project_name: string;
  project_org_tag: string | null;
  estimated_minutes: number | null;
  is_revenue_critical: boolean;
  deadline: string | null;
  created_at: string | null;
  completed_at: string | null;
  reminder_at: string | null;
  duration_mins: number | null;
}

export interface Project {
  id: number;
  name: string;
  org_tag: string | null;
  is_active: boolean;
  status: string;
}

export interface TaskFilters {
  search?: string;
  status?: string;
  priority?: string;
  projectId?: string;
  dueWindow?: string;
}

export interface TaskStats {
  open: number;
  dueToday: number;
  overdue: number;
  completedRecently: number;
}