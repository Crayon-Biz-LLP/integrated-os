export interface CanonicalPage {
  id: number;
  title: string;
  content: string | null;
  project_id: number | null;
  source_count: number | null;
  last_synth_at: string | null;
  updated_at: string | null;
  is_sparse: boolean | null;
  category: string | null;
}

export interface CanonicalPageListItem {
  id: number;
  title: string;
  project_id: number | null;
  source_count: number | null;
  last_synth_at: string | null;
  updated_at: string | null;
  is_sparse: boolean | null;
  category: string | null;
}

export interface GraphNode {
  id: number;
  label: string;
  type: string;
  canonical_page_id: number | null;
}

export interface GraphEdge {
  id: number;
  source_node_id: number;
  target_node_id: number;
  relationship: string;
}
