'use client';

import type { ProjectStats as ProjectStatsType } from '@/lib/projects/types';

export function ProjectsStats({ stats, loading }: { stats?: ProjectStatsType | null; loading?: boolean }) {
  const isLoading = loading ?? stats === null;

  if (isLoading) {
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
    { label: 'Total Projects', value: stats.totalActive + stats.totalArchived, color: 'text-foreground' },
    { label: 'Active Projects', value: stats.totalActive, color: 'text-primary' },
    { label: 'Open Tasks', value: stats.totalOpenTasks, color: 'text-primary' },
    { label: 'Idle Projects', value: stats.idleProjects, color: 'text-amber-500' },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {statCards.map((stat) => (
        <div
          key={stat.label}
          className="card-premium p-5 flex flex-col gap-1"
        >
          <p className="section-label">{stat.label}</p>
          <p className={`stat-number ${stat.color}`}>{stat.value}</p>
        </div>
      ))}
    </div>
  );
}