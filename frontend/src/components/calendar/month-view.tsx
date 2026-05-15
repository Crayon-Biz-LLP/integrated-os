'use client';

import type { CalendarEvent } from '@/lib/calendar/types';
import { startOfMonth, endOfMonth, startOfWeek, endOfWeek, addDays, format, isSameDay, isSameMonth } from 'date-fns';
import { cn } from '@/lib/utils';

interface MonthViewProps {
  events: CalendarEvent[];
  currentDate: Date;
  onDateChange: (date: Date) => void;
  onEventClick: (event: CalendarEvent) => void;
}

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function getDayEvents(day: Date, events: CalendarEvent[]): CalendarEvent[] {
  const dayStr = format(day, 'yyyy-MM-dd');
  return events.filter((e) => {
    const dt = e.start.dateTime || e.start.date;
    if (!dt) return false;
    return dt.substring(0, 10) === dayStr;
  });
}

export function MonthView({ events, currentDate, onDateChange, onEventClick }: MonthViewProps) {
  const monthStart = startOfMonth(currentDate);
  const monthEnd = endOfMonth(currentDate);
  const gridStart = startOfWeek(monthStart, { weekStartsOn: 1 });
  const gridEnd = endOfWeek(monthEnd, { weekStartsOn: 1 });
  const today = new Date();

  const cells: Date[] = [];
  let cursor = gridStart;
  while (cursor <= gridEnd) {
    cells.push(cursor);
    cursor = addDays(cursor, 1);
  }

  return (
    <div className="grid grid-cols-7 gap-px bg-border/40 rounded-lg overflow-hidden border border-border/40">
      {DAYS.map((d) => (
        <div key={d} className="bg-muted/30 px-2 py-1.5 text-center">
          <span className="text-[10px] font-medium text-muted-foreground uppercase tracking-wider">{d}</span>
        </div>
      ))}
      {cells.map((day) => {
        const dayEvents = getDayEvents(day, events);
        const isToday = isSameDay(day, today);
        const inMonth = isSameMonth(day, currentDate);

        return (
          <div
            key={day.toISOString()}
            className={cn(
              'min-h-[100px] bg-background p-1.5 transition-colors',
              !inMonth && 'bg-muted/10',
            )}
          >
            <button
              onClick={() => onDateChange(day)}
              className={cn(
                'w-full text-left mb-1',
                !inMonth && 'opacity-40',
              )}
            >
              <span
                className={cn(
                  'inline-flex items-center justify-center w-6 h-6 text-xs rounded-full',
                  isToday && 'bg-blue-600 text-white font-semibold',
                  !isToday && 'text-foreground',
                )}
              >
                {format(day, 'd')}
              </span>
            </button>
            <div className="space-y-0.5">
              {dayEvents.length <= 3
                ? dayEvents.map((event) => (
                    <button
                      key={event.id}
                      onClick={(e) => { e.stopPropagation(); onEventClick(event); }}
                      className={cn(
                        'w-full text-left px-1.5 py-0.5 rounded text-[11px] leading-tight transition-colors border-l-[2.5px] truncate block',
                        event.source === 'google'
                          ? 'border-l-blue-500 bg-blue-500/5 hover:bg-blue-500/10'
                          : 'border-l-purple-500 bg-purple-500/5 hover:bg-purple-500/10',
                      )}
                    >
                      {event.summary}
                    </button>
                  ))
                : (
                    <>
                      {dayEvents.slice(0, 2).map((event) => (
                        <button
                          key={event.id}
                          onClick={(e) => { e.stopPropagation(); onEventClick(event); }}
                          className={cn(
                            'w-full text-left px-1.5 py-0.5 rounded text-[11px] leading-tight transition-colors border-l-[2.5px] truncate block',
                            event.source === 'google'
                              ? 'border-l-blue-500 bg-blue-500/5 hover:bg-blue-500/10'
                              : 'border-l-purple-500 bg-purple-500/5 hover:bg-purple-500/10',
                          )}
                        >
                          {event.summary}
                        </button>
                      ))}
                      <div className="text-[10px] text-muted-foreground pl-1.5 pt-0.5">
                        +{dayEvents.length - 2} more
                      </div>
                    </>
                  )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
