'use client';

import { useEffect, useState } from 'react';
import { PeopleStats as PeopleStatsType } from '@/lib/people/types';
import { fetchPeopleStats } from '@/lib/people/api';
import { Users, Star, MessageSquare, Clock } from 'lucide-react';

export function PeopleStats() {
  const [stats, setStats] = useState<PeopleStatsType | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchPeopleStats().then((data) => {
      setStats(data);
      setLoading(false);
    });
  }, []);

  if (loading) {
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
        <div key={item.label} className="rounded-lg border bg-card p-3">
          <div className="flex items-center gap-2">
            <item.icon className="h-4 w-4 text-muted-foreground" />
            <span className="text-xs text-muted-foreground">{item.label}</span>
          </div>
          <p className="mt-1 text-xl font-semibold">{item.value}</p>
        </div>
      ))}
    </div>
  );
}
