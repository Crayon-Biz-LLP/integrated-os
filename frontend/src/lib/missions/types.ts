export interface Mission {
  id: number;
  title: string;
  description: string | null;
  status: string | null;
  created_at: string | null;
}

export interface MissionStats {
  total: number;
  active: number;
  completed: number;
  archived: number;
}
