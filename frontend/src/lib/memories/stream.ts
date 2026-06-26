import type { GraphNode, GraphEdge } from './types';

export interface StreamItem {
  id: number;
  content: string;
  memory_type: string | null;
  created_at: string;
}

export interface NeighborhoodResponse {
  center: GraphNode;
  nodes: GraphNode[];
  edges: GraphEdge[];
  danny_id?: string;
}

export interface StreamResponse {
  items: StreamItem[];
}

export interface EgoGraphResponse {
  center: GraphNode;
  nodes: GraphNode[];
  edges: GraphEdge[];
  danny_id: string;
}

export async function fetchNeighborhood(nodeId: string, signal?: AbortSignal): Promise<NeighborhoodResponse> {
  const res = await fetch(`/api/graph/neighborhood?node_id=${nodeId}`, { 
    cache: 'no-store',
    signal 
  });
  if (!res.ok) throw new Error('Failed to fetch neighborhood');
  return res.json();
}

export async function fetchEgoGraph(depth = 2, cap = 500, signal?: AbortSignal): Promise<EgoGraphResponse> {
  const res = await fetch(`/api/graph/ego?depth=${depth}&cap=${cap}`, {
    cache: 'no-store',
    signal,
  });
  if (!res.ok) throw new Error('Failed to fetch ego graph');
  return res.json();
}

export async function fetchMemoryStream(nodeId?: string, limit = 20, signal?: AbortSignal): Promise<StreamResponse> {
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

export interface EpisodeEntity {
  id: string;
  label: string;
  type: string;
}

export interface EpisodeRawMemory {
  id: number;
  content: string;
  memory_type: string | null;
  created_at: string;
}

export interface Episode {
  id: string;
  title: string;
  summary: string;
  memory_type: string | null;
  entities: EpisodeEntity[];
  timestamp: string;
  count: number;
  graph_node_ids: string[];
  memory_ids: number[];
  memories: EpisodeRawMemory[];
}

export interface EpisodesResponse {
  episodes: Episode[];
}

export async function fetchEpisodes(nodeId?: string, limit = 40, signal?: AbortSignal): Promise<EpisodesResponse> {
  const params = new URLSearchParams();
  if (nodeId) params.set("node_id", String(nodeId));
  params.set("limit", String(limit));
  const res = await fetch(`/api/episodes/stream?${params.toString()}`, {
    cache: 'no-store',
    signal,
  });
  if (!res.ok) throw new Error('Failed to fetch episodes');
  return res.json();
}

export async function resolveMemoryToEntity(memoryId: number, signal?: AbortSignal): Promise<string | null> {
  const res = await fetch(`/api/graph/resolve-memory?memory_id=${memoryId}`, {
    cache: 'no-store',
    signal,
  });
  if (!res.ok) return null;
  const data = await res.json();
  return data.entity_node_id || null;
}
