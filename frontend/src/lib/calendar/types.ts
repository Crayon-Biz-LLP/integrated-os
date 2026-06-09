export type CalendarSource = 'google' | 'outlook';

export type CalendarViewType = 'day' | 'week' | 'month' | 'agenda';

export interface CalendarEvent {
  id: string;
  summary: string;
  start: { dateTime: string; timeZone?: string; date?: string };
  end: { dateTime: string; timeZone?: string; date?: string };
  description?: string;
  source: CalendarSource;
  projectTag?: string;
  isPulseTask?: boolean;
}



