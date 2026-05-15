'use client';

import type { CalendarEvent } from '@/lib/calendar/types';
import { Calendar, Clock, Globe, FileText } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from '@/components/ui/sheet';

interface EventDetailSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  event: CalendarEvent | null;
}

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

function formatIsoDate(iso: string): string {
  const p = iso.substring(0, 10).split('-');
  if (p.length !== 3) return iso;
  const d = new Date(parseInt(p[0]), parseInt(p[1]) - 1, parseInt(p[2]));
  return `${DAYS[d.getDay()]}, ${MONTHS[d.getMonth()]} ${parseInt(p[2])}, ${p[0]}`;
}

function formatIsoTime(iso: string): string {
  const m = iso.match(/T(\d{2}):(\d{2})/);
  if (!m) return iso;
  const h = parseInt(m[1]);
  const min = m[2];
  const ampm = h >= 12 ? 'PM' : 'AM';
  const display = h === 0 ? 12 : h > 12 ? h - 12 : h;
  return `${display}:${min} ${ampm}`;
}

export function EventDetailSheet({ open, onOpenChange, event }: EventDetailSheetProps) {
  if (!event) return null;

  const startDate = event.start.dateTime || event.start.date;
  const endDate = event.end.dateTime || event.end.date;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-[500px] sm:max-w-[500px]">
        <SheetHeader>
          <SheetTitle className="text-lg font-semibold tracking-tight">{event.summary}</SheetTitle>
          <SheetDescription className="section-label mb-1">Calendar event details</SheetDescription>
        </SheetHeader>
        <div className="mt-6 space-y-5">
          <div className="flex items-center gap-2 text-sm">
            <Calendar className="h-4 w-4 text-muted-foreground shrink-0" />
            <span className="text-sm text-foreground">
              {startDate ? formatIsoDate(startDate) : 'No date'}
            </span>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <Clock className="h-4 w-4 text-muted-foreground shrink-0" />
            <span className="text-sm text-foreground">
              {startDate && startDate.includes('T')
                ? `${formatIsoTime(startDate)} – ${endDate && endDate.includes('T') ? formatIsoTime(endDate) : ''}`
                : 'All day'}
            </span>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <Globe className="h-4 w-4 text-muted-foreground shrink-0" />
            <span className="section-label mr-1">Source:</span>
            <Badge variant={event.source === 'google' ? 'default' : 'secondary'} className="text-[10px] px-1.5 py-0">
              {event.source === 'google' ? 'Google Calendar' : 'Outlook'}
            </Badge>
          </div>
          {event.description && (
            <div className="space-y-2">
              <div className="flex items-center gap-2 text-sm">
                <FileText className="h-4 w-4 text-muted-foreground shrink-0" />
                <span className="section-label">Description</span>
              </div>
              <p className="text-sm text-muted-foreground whitespace-pre-wrap ml-6">{event.description}</p>
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
