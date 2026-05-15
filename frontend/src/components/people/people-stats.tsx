'use client';

import type { PeopleStats as PeopleStatsType } from '@/lib/people/types';
import { Users, Star, MessageSquare, Clock } from 'lucide-react';

export function PeopleStats({ stats, loading }: { stats?: PeopleStatsType | null; loading?: boolean }) {
  const isLoading = loading ?? stats === null;

  if (isLoading) {
    return (
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {[1, 2, 3, 4].map((i) => (
          <div key={i} className="h-20 rounded-lg border bg-muted/20 animate-pulse" />
        ))}
      </div>
    );
  }

  if (!stats) return null;

  const items = [
    { label: 'Total People', value: stats.total, icon: Users },
    { label: 'High Priority', value: stats.highPriority, icon: Star },
    { label: 'With Open Tasks', value: stats.withActiveTasks, icon: MessageSquare },
    { label: 'Recently Added', value: stats.recentlyAdded, icon: Clock },
  ];

   return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {items.map((item) => (
        <div key={item.label} className="card-premium p-5 flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <item.icon className="h-4 w-4 text-muted-foreground" />
            <span className="section-label">{item.label}</span>
          </div>
          <p className={`stat-number ${
            item.label === 'High Priority' ? 'text-amber-500' :
            item.label === 'With Open Tasks' ? 'text-primary' :
            'text-foreground'
          }`}>{item.value}</p>
        </div>
      ))}
    </div>
  );
}
