export interface Project {
  id: number;
  name: string;
  status: string;
  context: string;
  description: string | null;
  created_at: string | null;
  org_tag: string | null;
  is_active: boolean;
  parent_project_id: number | null;
  parent_project_name?: string | null;
  keywords: string[];
  open_task_count: number;
}

export interface ProjectStats {
  totalActive: number;
  totalArchived: number;
  totalOpenTasks: number;
  idleProjects: number;
}

export interface ProjectFilters {
  search?: string;
  orgTag?: string;
  context?: string;
  status?: string;
}