import type { Mission } from './types';

export async function fetchMissions(): Promise<Mission[]> {
  const res = await fetch('/api/missions', { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch missions');
  return res.json();
}
