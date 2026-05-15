'use client';

import type { TaskStats as TaskStatsType } from '@/lib/tasks/types';

export function TasksStats({ stats, loading }: { stats?: TaskStatsType | null; loading?: boolean }) {
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
    { label: 'Open', value: stats.open, color: 'text-foreground' },
    { label: 'Due Today', value: stats.dueToday, color: 'text-amber-500' },
    { label: 'Overdue', value: stats.overdue, color: 'text-destructive' },
    { label: 'Completed', value: stats.completedRecently, color: 'text-primary' },
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