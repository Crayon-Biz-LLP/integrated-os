-- Add dismissed_at to resources table
ALTER TABLE public.resources ADD COLUMN IF NOT EXISTS dismissed_at TIMESTAMPTZ;
