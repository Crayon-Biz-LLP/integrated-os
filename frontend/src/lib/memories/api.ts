import { createClient } from '@/lib/supabase';
import { CanonicalPage, CanonicalPageListItem, GraphNode, GraphEdge } from './types';

const PAGE_LIST_COLUMNS = 'id,title,project_id,source_count,last_synth_at,updated_at,is_sparse,category';
const PAGE_DETAIL_COLUMNS = 'id,title,content,project_id,source_count,last_synth_at,updated_at,is_sparse,category';

export async function fetchPagesList(): Promise<CanonicalPageListItem[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from('canonical_pages')
    .select(PAGE_LIST_COLUMNS)
    .order('updated_at', { ascending: false });

  if (error) throw error;
  return data as CanonicalPageListItem[];
}

export async function fetchPageById(id: number): Promise<CanonicalPage | null> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from('canonical_pages')
    .select(PAGE_DETAIL_COLUMNS)
    .eq('id', id)
    .single();

  if (error) throw error;
  return data as CanonicalPage;
}

export async function fetchNodesByPageId(pageId: number): Promise<GraphNode[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from('graph_nodes')
    .select('id,label,type,canonical_page_id')
    .eq('canonical_page_id', pageId)
    .order('type', { ascending: true });

  if (error) throw error;
  return data as GraphNode[];
}

export async function fetchEdgesByPageId(pageId: number): Promise<GraphEdge[]> {
  const supabase = createClient();
  const { data: nodes, error: nodeError } = await supabase
    .from('graph_nodes')
    .select('id')
    .eq('canonical_page_id', pageId);
  if (nodeError || !nodes) return [];

  const nodeIds = nodes.map(n => n.id);
  if (nodeIds.length === 0) return [];

  const { data, error } = await supabase
    .from('graph_edges')
    .select('id,source_node_id,target_node_id,relationship')
    .or(`source_node_id.in.(${nodeIds.join(',')}),target_node_id.in.(${nodeIds.join(',')})`);

  if (error) return [];
  return data as GraphEdge[];
}

export async function fetchAllNodes(): Promise<GraphNode[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from('graph_nodes')
    .select('id,label,type,canonical_page_id')
    .order('type', { ascending: true });
  if (error) throw error;
  return data as GraphNode[];
}

export async function fetchAllEdges(): Promise<GraphEdge[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from('graph_edges')
    .select('id,source_node_id,target_node_id,relationship');
  if (error) throw error;
  return data as GraphEdge[];
}
