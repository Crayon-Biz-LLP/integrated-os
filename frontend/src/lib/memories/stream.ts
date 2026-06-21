import type { GraphNode, GraphEdge } from './types';

export interface StreamItem {
  id: number;
  title: string;
  content: string | null;
  updated_at: string | null;
  category: string | null;
}

export interface NeighborhoodResponse {
  center: GraphNode;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface StreamResponse {
  items: StreamItem[];
}

export async function fetchNeighborhood(nodeId: number, signal?: AbortSignal): Promise<NeighborhoodResponse> {
  const res = await fetch(`/api/graph/neighborhood?node_id=${nodeId}`, { 
    cache: 'no-store',
    signal 
  });
  if (!res.ok) throw new Error('Failed to fetch neighborhood');
  return res.json();
}

export async function fetchMemoryStream(nodeId?: number, limit = 20, signal?: AbortSignal): Promise<StreamResponse> {
  const params = new URLSearchParams();
  if (nodeId) params.set('node_id', String(nodeId));
  params.set('limit', String(limit));
  const res = await fetch(`/api/memories/stream?${params.toString()}`, { 
    cache: 'no-store',
    signal
  });
  if (!res.ok) throw new Error('Failed to fetch stream');
  return res.json();
}
