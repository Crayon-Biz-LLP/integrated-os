import { HealthStats } from './types';

export async function fetchHealthStats(): Promise<HealthStats> {
  const res = await fetch('/api/health');
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: 'Failed to fetch health stats' }));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  const data = await res.json();
  return data.stats;
}
