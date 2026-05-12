'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { HealthStats } from '@/lib/health/types';

interface HealthStatsProps {
  stats: HealthStats;
}

export function HealthStatsCards({ stats }: HealthStatsProps) {
  const totalRaw = Object.values(stats.rawDumps).reduce((a, b) => a + b, 0);

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">Pipeline</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold">{totalRaw}</div>
          <p className="text-xs text-muted-foreground">
            {stats.rawDumps.staged ?? 0} staged · {stats.rawDumps.processing ?? 0} processing ·{' '}
            {stats.rawDumps.embedding_failed ?? 0} failed
          </p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">Failed Queue</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold">{stats.failedQueue.total}</div>
          <p className="text-xs text-muted-foreground">
            {stats.failedQueue.unresolved} unresolved (max retries)
          </p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">Memories</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold">{stats.memories.total}</div>
          <p className="text-xs text-muted-foreground">
            +{stats.memories.recentWeek} this week
          </p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">Tasks</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold">{stats.tasks.open}</div>
          <p className="text-xs text-muted-foreground">
            {stats.tasks.closed} closed ({stats.tasks.open + stats.tasks.closed > 0
              ? Math.round(stats.tasks.closed / (stats.tasks.open + stats.tasks.closed) * 100)
              : 0}% completion)
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
