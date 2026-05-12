'use client';

import { useEffect, useState } from 'react';
import { HealthStatsCards } from '@/components/health/health-stats';
import { FailedQueueTable } from '@/components/health/health-failed-queue';
import { ErrorsTable } from '@/components/health/health-errors';
import { HealthStats } from '@/lib/health/types';
import { fetchHealthStats } from '@/lib/health/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

export default function HealthPage() {
  const [stats, setStats] = useState<HealthStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await fetchHealthStats();
        if (!cancelled) setStats(data);
      } catch (err: any) {
        if (!cancelled) setError(err.message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => { cancelled = true; };
  }, []);

  if (loading) {
    return (
      <div className="flex-1 space-y-4 p-4 md:p-6">
        <Skeleton className="h-8 w-48" />
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          {[...Array(4)].map((_, i) => <Skeleton key={i} className="h-28" />)}
        </div>
        <Skeleton className="h-64" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex-1 space-y-4 p-4 md:p-6">
        <h1 className="text-2xl font-bold">Health Dashboard</h1>
        <p className="text-red-500">Failed to load health stats: {error}</p>
      </div>
    );
  }

  if (!stats) return null;

  return (
    <div className="flex-1 space-y-6 p-4 md:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Health Dashboard</h1>
        <p className="text-xs text-muted-foreground">
          Last refreshed: {new Date().toLocaleString()}
        </p>
      </div>

      <HealthStatsCards stats={stats} />

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Failed Queue (DLQ)</CardTitle>
          </CardHeader>
          <CardContent>
            <FailedQueueTable items={stats.failedQueue.recentItems} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Recent Errors</CardTitle>
          </CardHeader>
          <CardContent>
            <ErrorsTable errors={stats.auditLogs.recentErrors} />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
