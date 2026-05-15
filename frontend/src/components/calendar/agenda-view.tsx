'use client';

import type { CalendarEvent } from '@/lib/calendar/types';
import { format } from 'date-fns';
import { Badge } from '@/components/ui/badge';
import { Clock, CalendarDays } from 'lucide-react';

interface AgendaViewProps {
  events: CalendarEvent[];
  date: Date;
  onEventClick: (event: CalendarEvent) => void;
}

function formatTime(iso: string): string {
  const m = iso.match(/T(\d{2}):(\d{2})/);
  if (!m) return iso;
  const h = parseInt(m[1]);
  const min = m[2];
  const ampm = h >= 12 ? 'PM' : 'AM';
  const display = h === 0 ? 12 : h > 12 ? h - 12 : h;
  return `${display}:${min} ${ampm}`;
}

export function AgendaView({ events, date, onEventClick }: AgendaViewProps) {
  const dayLabel = format(date, 'EEEE, MMMM d, yyyy');
  const sorted = [...events].sort((a, b) => {
    const aT = a.start.dateTime || a.start.date || '';
    const bT = b.start.dateTime || b.start.date || '';
    return aT.localeCompare(bT);
  });

  if (sorted.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
        <CalendarDays className="h-8 w-8 mb-3 opacity-40" />
        <p className="text-sm font-medium">{dayLabel}</p>
        <p className="text-xs mt-1">No events scheduled</p>
      </div>
    );
  }

  return (
    <div className="space-y-1">
      <p className="text-xs text-muted-foreground mb-3 font-medium">{dayLabel} — {sorted.length} event{sorted.length !== 1 ? 's' : ''}</p>
      {sorted.map((event) => {
        const isTimed = !!event.start.dateTime;
        return (
          <button
            key={event.id}
            onClick={() => onEventClick(event)}
            className="w-full text-left flex items-start gap-3 px-3 py-2.5 rounded-lg hover:bg-muted/50 transition-colors group"
          >
            <div className="w-20 shrink-0 pt-0.5">
              {isTimed ? (
                <span className="text-xs tabular-nums text-muted-foreground font-medium flex items-center gap-1">
                  <Clock className="h-3 w-3" />
                  {formatTime(event.start.dateTime)}
                </span>
              ) : (
                <span className="text-[11px] text-muted-foreground">All day</span>
              )}
            </div>
            <div className="flex-1 min-w-0">
              <span className="text-sm font-medium block truncate group-hover:text-foreground transition-colors">
                {event.summary}
              </span>
            </div>
            <Badge
              variant={event.source === 'google' ? 'default' : 'secondary'}
              className="text-[9px] px-1.5 py-0 h-4 shrink-0 mt-0.5"
            >
              {event.source === 'google' ? 'Google' : 'Outlook'}
            </Badge>
          </button>
        );
      })}
    </div>
  );
}
