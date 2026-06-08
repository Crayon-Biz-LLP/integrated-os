-- Add recurrence column to tasks table for iCalendar RRULE support
ALTER TABLE public.tasks ADD COLUMN recurrence text DEFAULT NULL;