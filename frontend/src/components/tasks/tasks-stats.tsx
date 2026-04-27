'use client';

import { useEffect, useState } from 'react';
import { TaskStats as TaskStatsType } from '@/lib/tasks/types';
import { fetchTaskStats } from '@/lib/tasks/api';

export function TasksStats() {
  const [stats, setStats] = useState<TaskStatsType | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchTaskStats().then((data) => {
      setStats(data);
      setLoading(false);
    });
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
    { label: 'Open', value: stats.open, color: 'text-foreground' },
    { label: 'Due Today', value: stats.dueToday, color: 'text-amber-600' },
    { label: 'Overdue', value: stats.overdue, color: 'text-red-600' },
    { label: 'Completed', value: stats.completedRecently, color: 'text-green-600' },
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