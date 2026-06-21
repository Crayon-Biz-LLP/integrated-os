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
  id: string;
  label: string;
  type: string;
  canonical_page_id: number | null;
}

export interface GraphEdge {
  id: string;
  source_node_id: string;
  target_node_id: string;
  relationship: string;
}
