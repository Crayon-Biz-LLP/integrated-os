'use client';

import type { CalendarEvent } from '@/lib/calendar/types';
import { startOfWeek, addDays, format } from 'date-fns';
import { cn } from '@/lib/utils';

interface WeekViewProps {
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

function formatIsoTimeShort(iso: string): string {
  const m = iso.match(/T(\d{2}):(\d{2})/);
  if (!m) return '';
  const h = parseInt(m[1]);
  const min = m[2];
  const ampm = h >= 12 ? 'PM' : 'AM';
  const display = h === 0 ? 12 : h > 12 ? h - 12 : h;
  return `${display}:${min} ${ampm}`;
}

export function WeekView({ events, currentDate, onDateChange, onEventClick }: WeekViewProps) {
  const weekStart = startOfWeek(currentDate, { weekStartsOn: 1 });
  const today = new Date();

  return (
    <div className="grid grid-cols-7 gap-px bg-border/40 rounded-lg overflow-hidden border border-border/40">
      {DAYS.map((dayName, i) => {
        const day = addDays(weekStart, i);
        const isToday = format(day, 'yyyy-MM-dd') === format(today, 'yyyy-MM-dd');
        const dayEvents = getDayEvents(day, events);

        return (
          <div
            key={i}
            className={cn(
              'min-h-[180px] bg-background p-2',
              isToday && 'bg-blue-500/5',
            )}
          >
            <button
              onClick={() => onDateChange(day)}
              className={cn(
                'w-full text-center mb-2 pb-1 border-b border-border/40',
                isToday && 'border-b-blue-500',
              )}
            >
              <div className="text-[10px] text-muted-foreground uppercase font-medium">{dayName}</div>
              <div className={cn(
                'text-sm font-semibold mt-0.5',
                isToday && 'text-blue-600',
              )}>
                {format(day, 'd')}
              </div>
            </button>
            <div className="space-y-1">
              {dayEvents.length <= 4
                ? dayEvents.map((event) => (
                    <button
                      key={event.id}
                      onClick={() => onEventClick(event)}
                      className={cn(
                        'w-full text-left px-2 py-1 rounded text-[11px] leading-tight transition-colors border-l-2',
                        event.source === 'google'
                          ? 'border-l-blue-500 bg-blue-500/5 hover:bg-blue-500/10'
                          : 'border-l-purple-500 bg-purple-500/5 hover:bg-purple-500/10',
                      )}
                    >
                      {event.start.dateTime && (
                        <span className="block text-[10px] text-muted-foreground tabular-nums">
                          {formatIsoTimeShort(event.start.dateTime)}
                        </span>
                      )}
                      <span className="font-medium">{event.summary}</span>
                    </button>
                  ))
                : (
                    <>
                      {dayEvents.slice(0, 3).map((event) => (
                        <button
                          key={event.id}
                          onClick={() => onEventClick(event)}
                          className={cn(
                            'w-full text-left px-2 py-1 rounded text-[11px] leading-tight transition-colors border-l-2',
                            event.source === 'google'
                              ? 'border-l-blue-500 bg-blue-500/5 hover:bg-blue-500/10'
                              : 'border-l-purple-500 bg-purple-500/5 hover:bg-purple-500/10',
                          )}
                        >
                          {event.summary}
                        </button>
                      ))}
                      <div className="text-[10px] text-muted-foreground text-center pt-0.5">
                        +{dayEvents.length - 3} more
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
