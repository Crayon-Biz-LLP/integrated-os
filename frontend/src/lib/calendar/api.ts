import type { CalendarEvent } from './types';

const API_BASE = process.env.NEXT_PUBLIC_BACKEND_URL || '';

export async function fetchCalendarEvents(date?: string): Promise<CalendarEvent[]> {
  const params = date ? `?date=${date}` : '?date=today';
  const res = await fetch(`${API_BASE}/api/calendar-events${params}`);
  if (!res.ok) throw new Error('Failed to fetch calendar events');
  const data = await res.json();
  return data.events || [];
}

export async function fetchEventsByRange(start: string, end: string): Promise<CalendarEvent[]> {
  const res = await fetch(`${API_BASE}/api/calendar-events?start=${start}&end=${end}`);
  if (!res.ok) throw new Error('Failed to fetch calendar events by range');
  const data = await res.json();
  return data.events || [];
}