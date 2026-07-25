export interface Task {
  id: number;
  title: string;
  status: string;
  priority: string;
  organization_id?: string | null;
  organization_name?: string | null;
  estimated_minutes: number | null;
  is_revenue_critical: boolean;
  deadline: string | null;
  created_at: string | null;
  completed_at: string | null;
  reminder_at: string | null;
  duration_mins: number | null;
  recurrence: string | null;
}

export interface TaskFilters {
  search?: string;
  status?: string;
  priority?: string;
  orgId?: string;
  dueWindow?: string;
}

export interface TaskStats {
  open: number;
  dueToday: number;
  overdue: number;
  completedRecently: number;
}