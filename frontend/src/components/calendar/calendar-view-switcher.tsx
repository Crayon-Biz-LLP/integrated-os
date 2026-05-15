'use client';

import type { CalendarViewType } from '@/lib/calendar/types';
import { cn } from '@/lib/utils';

interface CalendarViewSwitcherProps {
  view: CalendarViewType;
  onViewChange: (view: CalendarViewType) => void;
}

const views: { value: CalendarViewType; label: string }[] = [
  { value: 'day', label: 'Day' },
  { value: 'week', label: 'Week' },
  { value: 'month', label: 'Month' },
  { value: 'agenda', label: 'Agenda' },
];

export function CalendarViewSwitcher({ view, onViewChange }: CalendarViewSwitcherProps) {
  return (
    <div className="inline-flex items-center rounded-lg border bg-muted/40 p-0.5">
      {views.map((v) => (
        <button
          key={v.value}
          onClick={() => onViewChange(v.value)}
          className={cn(
            'px-3 py-1.5 text-xs font-medium rounded-md transition-all',
            view === v.value
              ? 'bg-background text-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground',
          )}
        >
          {v.label}
        </button>
      ))}
    </div>
  );
}
