'use client';

import { useState, useMemo } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import type { Cluster, ClusterStats } from '@/lib/clusters/types';
import { Target, Archive, CheckCircle, Activity } from 'lucide-react';

const statusColors: Record<string, string> = {
  active: 'bg-green-500/10 text-green-600 border-green-500/20',
  completed: 'bg-blue-500/10 text-blue-600 border-blue-500/20',
  archived: 'bg-muted text-muted-foreground border-border',
};

const statusLabels: Record<string, string> = {
  active: 'Active',
  completed: 'Completed',
  archived: 'Archived',
};

export function ClustersShell({
  initialClusters,
  initialStats,
}: {
  initialClusters: Cluster[];
  initialStats: ClusterStats;
}) {
  const [clusters] = useState(initialClusters);
  const [statusFilter, setStatusFilter] = useState<string | null>(null);

  const filtered = useMemo(
    () => statusFilter ? clusters.filter((m) => m.status === statusFilter) : clusters,
    [clusters, statusFilter]
  );

  const statsCards = [
    { label: 'Total', value: initialStats.total, icon: Target, color: 'text-primary' },
    { label: 'Active', value: initialStats.active, icon: Activity, color: 'text-green-500' },
    { label: 'Completed', value: initialStats.completed, icon: CheckCircle, color: 'text-blue-500' },
    { label: 'Archived', value: initialStats.archived, icon: Archive, color: 'text-muted-foreground' },
  ];

  return (
    <div className="p-4 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Clusters</h1>
      <p className="text-sm text-muted-foreground/70 mt-0.5">
        Strategic clusters and initiatives
      </p>

      <div className="grid gap-4 md:grid-cols-4 mt-6">
        {statsCards.map((stat) => {
          const Icon = stat.icon;
          return (
            <Card key={stat.label}>
              <CardContent className="flex items-center gap-3 p-4">
                <Icon className={`h-5 w-5 ${stat.color}`} />
                <div>
                  <p className="text-2xl font-bold">{stat.value}</p>
                  <p className="text-xs text-muted-foreground">{stat.label}</p>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      <div className="flex gap-1.5 mt-6 mb-4">
        <button
          onClick={() => setStatusFilter(null)}
          className={`text-xs rounded-full px-2.5 py-1 transition-colors ${
            statusFilter === null
              ? 'bg-accent text-foreground'
              : 'bg-transparent text-muted-foreground/70 hover:text-foreground/80'
          }`}
        >
          All
        </button>
        {['active', 'completed', 'archived'].map((s) => (
          <button
            key={s}
            onClick={() => setStatusFilter(s)}
            className={`text-xs rounded-full px-2.5 py-1 transition-colors ${
              statusFilter === s
                ? 'bg-accent text-foreground'
                : 'bg-transparent text-muted-foreground/70 hover:text-foreground/80'
            }`}
          >
            {statusLabels[s] || s}
          </button>
        ))}
      </div>

      {filtered.length === 0 && (
        <div className="rounded-md border p-8 text-center text-muted-foreground">
          <Target className="h-8 w-8 mx-auto mb-2 text-muted-foreground/50" />
          No clusters found.
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {filtered.map((cluster) => (
          <Card key={cluster.id}>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base font-semibold">{cluster.title}</CardTitle>
                <Badge
                  variant="outline"
                  className={`text-xs ${statusColors[cluster.status || ''] || 'bg-muted text-muted-foreground'}`}
                >
                  {statusLabels[cluster.status || ''] || cluster.status || 'Unknown'}
                </Badge>
              </div>
            </CardHeader>
            <CardContent>
              {cluster.description && (
                <p className="text-sm text-muted-foreground line-clamp-3">{cluster.description}</p>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
