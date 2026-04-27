'use client';

import { useEffect, useState } from 'react';
import { ResourceStats } from '@/lib/resources/types';
import { fetchResourceStats } from '@/lib/resources/api';

export function ResourcesStats() {
  const [stats, setStats] = useState<ResourceStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchResourceStats()
      .then((data) => {
        setStats(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {[1, 2, 3, 4].map((i) => (
          <div
            key={i}
            className="h-20 rounded-lg border bg-muted/20 animate-pulse"
          />
        ))}
      </div>
    );
  }

  if (!stats) return null;

  const statCards = [
    { label: 'Total Resources', value: stats.totalResources, color: 'text-foreground' },
    { label: 'Active Missions With Resources', value: stats.activeMissionsWithResources, color: 'text-blue-600' },
    { label: 'Unmapped Resources', value: stats.unmappedResources, color: 'text-amber-600' },
    { label: 'Added in Last 30 Days', value: stats.recentResources, color: 'text-green-600' },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {statCards.map((stat) => (
        <div
          key={stat.label}
          className="rounded-lg border bg-card p-3"
        >
          <p className="text-xs text-muted-foreground">{stat.label}</p>
          <p className={`text-2xl font-semibold ${stat.color}`}>{stat.value}</p>
        </div>
      ))}
    </div>
  );
}
