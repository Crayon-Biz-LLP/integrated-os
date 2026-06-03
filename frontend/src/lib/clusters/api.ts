import type { Cluster } from './types';

export async function fetchClusters(): Promise<Cluster[]> {
  const res = await fetch('/api/clusters', { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch clusters');
  return res.json();
}
