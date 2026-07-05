import type { TelemetryStats } from './types';

export async function fetchTelemetryStats(): Promise<TelemetryStats> {
  const res = await fetch('/api/telemetry');
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: 'Failed to fetch telemetry stats' }));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  const data = await res.json();
  return data.stats;
}
