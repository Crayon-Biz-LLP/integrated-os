'use client';

import { useEffect, useState } from 'react';
import { ProjectStats as ProjectStatsType } from '@/lib/projects/types';
import { fetchProjectStats } from '@/lib/projects/api';

export function ProjectsStats() {
  const [stats, setStats] = useState<ProjectStatsType | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchProjectStats()
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
    { label: 'Active Projects', value: stats.totalActive, color: 'text-foreground' },
    { label: 'Archived', value: stats.totalArchived, color: 'text-muted-foreground' },
    { label: 'Open Tasks', value: stats.totalOpenTasks, color: 'text-blue-600' },
    { label: 'Idle Projects', value: stats.idleProjects, color: 'text-amber-600' },
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