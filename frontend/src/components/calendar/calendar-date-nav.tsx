'use client';

import { ChevronLeft, ChevronRight, CalendarDays } from 'lucide-react';
import { format, addDays, addWeeks, addMonths, startOfWeek, endOfWeek } from 'date-fns';
import type { CalendarViewType } from '@/lib/calendar/types';

interface CalendarDateNavProps {
  currentDate: Date;
  view: CalendarViewType;
  onDateChange: (date: Date) => void;
  onToday: () => void;
}

export function CalendarDateNav({ currentDate, view, onDateChange, onToday }: CalendarDateNavProps) {
  function navigate(direction: -1 | 1) {
    if (view === 'day') {
      onDateChange(addDays(currentDate, direction));
    } else if (view === 'week') {
      onDateChange(addWeeks(currentDate, direction));
    } else if (view === 'month') {
      onDateChange(addMonths(currentDate, direction));
    }
  }

  function getLabel(): string {
    if (view === 'day') {
      return format(currentDate, 'EEEE, MMMM d, yyyy');
    }
    if (view === 'month') {
      return format(currentDate, 'MMMM yyyy');
    }
    const start = startOfWeek(currentDate, { weekStartsOn: 1 });
    const end = endOfWeek(currentDate, { weekStartsOn: 1 });
    return `${format(start, 'MMM d')} – ${format(end, 'MMM d, yyyy')}`;
  }

  return (
    <div className="flex items-center gap-3">
      <button
        onClick={() => navigate(-1)}
        className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
        aria-label="Previous"
      >
        <ChevronLeft className="h-4 w-4" />
      </button>
      <div className="flex items-center gap-2 min-w-0">
        <CalendarDays className="h-4 w-4 text-muted-foreground shrink-0" />
        <span className="text-sm font-medium whitespace-nowrap">{getLabel()}</span>
      </div>
      <button
        onClick={() => navigate(1)}
        className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
        aria-label="Next"
      >
        <ChevronRight className="h-4 w-4" />
      </button>
      <button
        onClick={onToday}
        className="ml-2 px-2.5 py-1 text-xs font-medium rounded-md border hover:bg-muted transition-colors"
      >
        Today
      </button>
    </div>
  );
}
