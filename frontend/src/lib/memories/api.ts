import type { CanonicalPage, CanonicalPageListItem, GraphNode, GraphEdge } from './types';

export async function fetchPagesList(): Promise<CanonicalPageListItem[]> {
  const res = await fetch('/api/memories?type=pages', { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch pages list');
  return res.json();
}

export async function fetchPageById(id: number): Promise<CanonicalPage | null> {
  const res = await fetch(`/api/memories?type=page&id=${id}`, { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch page');
  return res.json();
}

export async function fetchNodesByPageId(pageId: number): Promise<GraphNode[]> {
  const res = await fetch(`/api/memories?type=nodes&pageId=${pageId}`, { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch nodes');
  return res.json();
}

export async function fetchEdgesByPageId(pageId: number): Promise<GraphEdge[]> {
  const res = await fetch(`/api/memories?type=edges&pageId=${pageId}`, { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch edges');
  return res.json();
}

export async function fetchAllNodes(): Promise<GraphNode[]> {
  const res = await fetch('/api/memories?type=nodes', { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch all nodes');
  return res.json();
}

export async function fetchAllEdges(): Promise<GraphEdge[]> {
  const res = await fetch('/api/memories?type=edges', { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch all edges');
  return res.json();
}
