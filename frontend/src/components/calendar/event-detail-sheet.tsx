'use client';

import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import type { CalendarEvent } from '@/lib/calendar/types';
import { format, parseISO } from 'date-fns';
import { Calendar, Clock, Globe, FileText } from 'lucide-react';

interface EventDetailSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  event: CalendarEvent | null;
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
              {startDate
                ? format(parseISO(startDate.replace('+05:30', 'Z').replace(' ', 'T')), 'EEEE, MMMM d, yyyy')
                : 'No date'}
            </span>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <Clock className="h-4 w-4 text-muted-foreground shrink-0" />
            <span className="text-sm text-foreground">
              {startDate
                ? `${format(parseISO(startDate.replace('+05:30', 'Z').replace(' ', 'T')), 'h:mm a')} – ${
                    endDate
                      ? format(parseISO(endDate.replace('+05:30', 'Z').replace(' ', 'T')), 'h:mm a')
                      : ''
                  }`
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
