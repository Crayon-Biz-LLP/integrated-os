'use client';

import { useState, useEffect, useCallback } from 'react';
import type { CalendarEvent, CalendarViewType, CalendarSource } from '@/lib/calendar/types';
import { fetchCalendarEvents, fetchEventsByRange } from '@/lib/calendar/api';
import { CalendarViewSwitcher } from '@/components/calendar/calendar-view-switcher';
import { CalendarDateNav } from '@/components/calendar/calendar-date-nav';
import { CalendarStats } from '@/components/calendar/calendar-stats';
import { DayView } from '@/components/calendar/day-view';
import { WeekView } from '@/components/calendar/week-view';
import { MonthView } from '@/components/calendar/month-view';
import { AgendaView } from '@/components/calendar/agenda-view';
import { EventDetailSheet } from '@/components/calendar/event-detail-sheet';
import { startOfWeek, endOfWeek, startOfMonth, endOfMonth, format } from 'date-fns';
import { cn } from '@/lib/utils';

export default function CalendarPage() {
  const [view, setView] = useState<CalendarViewType>('day');
  const [currentDate, setCurrentDate] = useState(new Date());
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedEvent, setSelectedEvent] = useState<CalendarEvent | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [sourceFilter, setSourceFilter] = useState<CalendarSource | 'all'>('all');

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      if (view === 'month') {
        const start = startOfMonth(currentDate);
        const end = endOfMonth(currentDate);
        const data = await fetchEventsByRange(
          format(start, 'yyyy-MM-dd'),
          format(end, 'yyyy-MM-dd'),
        );
        setEvents(data);
      } else if (view === 'week') {
        const start = startOfWeek(currentDate, { weekStartsOn: 1 });
        const end = endOfWeek(currentDate, { weekStartsOn: 1 });
        const data = await fetchEventsByRange(
          format(start, 'yyyy-MM-dd'),
          format(end, 'yyyy-MM-dd'),
        );
        setEvents(data);
      } else {
        const data = await fetchCalendarEvents(format(currentDate, 'yyyy-MM-dd'));
        setEvents(data);
      }
    } catch (e) {
      console.error('Failed to fetch calendar events:', e);
      setEvents([]);
    } finally {
      setLoading(false);
    }
  }, [view, currentDate]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  function handleEventClick(event: CalendarEvent) {
    setSelectedEvent(event);
    setSheetOpen(true);
  }

  function handleNavigateToDay(date: Date) {
    setCurrentDate(date);
    setView('day');
  }

  function goToday() {
    setCurrentDate(new Date());
  }

  const filteredEvents = sourceFilter === 'all' ? events : events.filter((e) => e.source === sourceFilter);

  const filterOptions: { value: CalendarSource | 'all'; label: string }[] = [
    { value: 'all', label: 'All' },
    { value: 'google', label: 'Google' },
    { value: 'outlook', label: 'Outlook' },
  ];

  return (
    <div className="p-6 space-y-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <CalendarViewSwitcher view={view} onViewChange={setView} />
          <CalendarDateNav
            currentDate={currentDate}
            view={view}
            onDateChange={setCurrentDate}
            onToday={goToday}
          />
        </div>
      </div>

      <div className="flex items-center justify-between">
        <CalendarStats
          events={filteredEvents}
          date={currentDate}
          viewLabel={view}
        />
        <div className="flex items-center gap-1.5">
          {filterOptions.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setSourceFilter(opt.value)}
              className={cn(
                'px-2.5 py-1 text-[11px] font-medium rounded-md transition-colors',
                sourceFilter === opt.value
                  ? 'bg-muted text-foreground'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-16 text-sm text-muted-foreground">
          Loading events...
        </div>
      ) : view === 'day' ? (
        <DayView events={filteredEvents} date={currentDate} onEventClick={handleEventClick} />
      ) : view === 'week' ? (
        <WeekView
          events={filteredEvents}
          currentDate={currentDate}
          onDateChange={handleNavigateToDay}
          onEventClick={handleEventClick}
        />
      ) : view === 'month' ? (
        <MonthView
          events={filteredEvents}
          currentDate={currentDate}
          onDateChange={handleNavigateToDay}
          onEventClick={handleEventClick}
        />
      ) : (
        <AgendaView events={filteredEvents} date={currentDate} onEventClick={handleEventClick} />
      )}

      <EventDetailSheet
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        event={selectedEvent}
      />
    </div>
  );
}
