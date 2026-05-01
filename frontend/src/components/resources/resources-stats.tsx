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
    { label: 'Total Resources', value: stats.totalResources, colorClass: 'text-foreground' },
    { label: 'Active Missions With Resources', value: stats.activeMissionsWithResources, colorClass: 'text-primary' },
    { label: 'Unmapped Resources', value: stats.unmappedResources, colorClass: 'text-foreground' },
    { label: 'Added in Last 30 Days', value: stats.recentResources, colorClass: 'text-primary' },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {statCards.map((stat) => (
        <div
          key={stat.label}
          className="card-premium p-5 flex flex-col gap-1"
        >
          <p className="section-label">{stat.label}</p>
          <p className={`stat-number ${stat.colorClass}`}>{stat.value}</p>
        </div>
      ))}
    </div>
  );
}
