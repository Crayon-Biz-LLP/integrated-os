'use client';

import { TaskStats } from '@/lib/tasks/types';
import { EmailStats } from '@/lib/emails/types';

interface StatsCardsProps {
  taskStats: TaskStats;
  emailStats: EmailStats;
}

export function StatsCards({ taskStats, emailStats }: StatsCardsProps) {
  const cards = [
    { label: 'Open Tasks', value: taskStats.open, color: 'text-foreground' },
    { label: 'Due Today', value: taskStats.dueToday, color: 'text-amber-500' },
    { label: 'Overdue', value: taskStats.overdue, color: 'text-destructive' },
    { label: 'Pending Emails', value: emailStats.pending_tasks, color: 'text-blue-500' },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
      {cards.map((card) => (
        <div key={card.label} className="card-premium p-5 flex flex-col gap-1">
          <p className="section-label">{card.label}</p>
          <p className={`stat-number ${card.color}`}>{card.value}</p>
        </div>
      ))}
    </div>
  );
}
