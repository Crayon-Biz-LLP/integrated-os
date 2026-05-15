'use client';

import type { CalendarEvent } from '@/lib/calendar/types';
import { Calendar, Globe, Cloud } from 'lucide-react';

interface CalendarStatsProps {
  events: CalendarEvent[];
  date: Date;
  viewLabel: string;
}

export function CalendarStats({ events, date, viewLabel }: CalendarStatsProps) {
  const total = events.length;
  const google = events.filter((e) => e.source === 'google').length;
  const outlook = events.filter((e) => e.source === 'outlook').length;

  if (total === 0) return null;

  return (
    <div className="flex items-center gap-4 text-xs text-muted-foreground">
      <div className="flex items-center gap-1.5">
        <Calendar className="h-3.5 w-3.5" />
        <span className="font-medium tabular-nums">{total}</span>
        <span>event{total !== 1 ? 's' : ''}</span>
      </div>
      <div className="flex items-center gap-1.5">
        <Globe className="h-3.5 w-3.5 text-blue-500" />
        <span className="tabular-nums">{google}</span>
        <span>Google</span>
      </div>
      <div className="flex items-center gap-1.5">
        <Cloud className="h-3.5 w-3.5 text-purple-500" />
        <span className="tabular-nums">{outlook}</span>
        <span>Outlook</span>
      </div>
    </div>
  );
}
