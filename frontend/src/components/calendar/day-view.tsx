'use client';

import type { CalendarEvent } from '@/lib/calendar/types';
import { parseISO, format } from 'date-fns';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { Clock } from 'lucide-react';

interface DayViewProps {
  events: CalendarEvent[];
  date: Date;
  onEventClick: (event: CalendarEvent) => void;
}

const HOURS = Array.from({ length: 16 }, (_, i) => i + 6);

const SOURCE_STYLES = {
  google: 'border-l-blue-500 bg-blue-500/5 hover:bg-blue-500/10',
  outlook: 'border-l-purple-500 bg-purple-500/5 hover:bg-purple-500/10',
} as const;

const SOURCE_BADGE = {
  google: { variant: 'default' as const, label: 'Google' },
  outlook: { variant: 'secondary' as const, label: 'Outlook' },
} as const;

function getEventHour(time: string): number {
  try {
    const d = parseISO(time.replace('+05:30', 'Z').replace(' ', 'T'));
    return d.getHours() + d.getMinutes() / 60;
  } catch {
    return 6;
  }
}

function formatEventTime(time: string): string {
  try {
    const d = parseISO(time.replace('+05:30', 'Z').replace(' ', 'T'));
    return format(d, 'h:mm a');
  } catch {
    return time;
  }
}

export function DayView({ events, date, onEventClick }: DayViewProps) {
  const allDayEvents = events.filter((e) => !e.start.dateTime && e.start.date);

  return (
    <div className="relative">
      {allDayEvents.length > 0 && (
        <div className="mb-3 space-y-1">
          <span className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">All day</span>
          {allDayEvents.map((event) => (
            <button
              key={event.id}
              onClick={() => onEventClick(event)}
              className="w-full text-left px-3 py-2 rounded-lg border border-dashed border-muted-foreground/20 bg-muted/30 hover:bg-muted/50 text-sm transition-colors"
            >
              {event.summary}
            </button>
          ))}
        </div>
      )}
      <div className="space-y-0.5">
        {HOURS.map((hour) => {
          const slotStart = hour;
          const slotEnd = hour + 1;
          const slotEvents = events.filter((e) => {
            if (!e.start.dateTime) return false;
            const h = getEventHour(e.start.dateTime);
            return h >= slotStart && h < slotEnd;
          });

          return (
            <div key={hour} className="flex group">
              <div className="w-14 shrink-0 pt-1 text-right pr-3">
                <span className="text-[11px] text-muted-foreground tabular-nums font-medium">
                  {hour === 0 ? '12 AM' : hour < 12 ? `${hour} AM` : hour === 12 ? '12 PM' : `${hour - 12} PM`}
                </span>
              </div>
              <div className={cn('flex-1 min-h-[40px] border-t border-border/40 relative py-0.5', slotEvents.length === 0 && 'group-hover:bg-muted/20')}>
                {slotEvents.map((event) => {
                  const startHour = getEventHour(event.start.dateTime);
                  const offsetMinutes = (startHour - slotStart) * 60;
                  return (
                    <button
                      key={event.id}
                      onClick={() => onEventClick(event)}
                      className={cn(
                        'w-full text-left px-2.5 py-1.5 rounded-md border-l-[3px] mb-0.5 transition-colors text-sm',
                        SOURCE_STYLES[event.source],
                      )}
                      style={offsetMinutes > 0 ? { marginTop: `${(offsetMinutes / 60) * 40}px` } : undefined}
                    >
                      <div className="flex items-center gap-2">
                        <Clock className="h-3 w-3 text-muted-foreground shrink-0" />
                        <span className="text-xs tabular-nums text-muted-foreground">
                          {formatEventTime(event.start.dateTime)}
                        </span>
                        <Badge variant={SOURCE_BADGE[event.source].variant} className="text-[9px] px-1 py-0 h-4 ml-auto">
                          {SOURCE_BADGE[event.source].label}
                        </Badge>
                      </div>
                      <span className="font-medium text-xs leading-tight block mt-0.5">{event.summary}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
