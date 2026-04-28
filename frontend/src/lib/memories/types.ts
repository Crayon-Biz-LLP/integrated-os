export interface CanonicalPage {
  id: number;
  title: string;
  content: string | null;
  project_id: number | null;
  source_count: number | null;
  last_synth_at: string | null;
  updated_at: string | null;
  is_sparse: boolean | null;
}

export interface CanonicalPageListItem {
  id: number;
  title: string;
  project_id: number | null;
  source_count: number | null;
  last_synth_at: string | null;
  updated_at: string | null;
  is_sparse: boolean | null;
}
