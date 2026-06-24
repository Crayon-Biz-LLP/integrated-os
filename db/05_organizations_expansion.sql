-- Phase 1: Organizations Expansion (Additive only)

-- 1. Expand organizations table
ALTER TABLE public.organizations 
  ADD COLUMN parent_organization_id UUID REFERENCES public.organizations(id),
  ADD COLUMN org_type TEXT CHECK (org_type IN (
    'holding', 'company', 'client', 'sub_org', 'domain', 'vendor', 'partner'
  )),
  ADD COLUMN description TEXT,
  ADD COLUMN is_active BOOLEAN DEFAULT true,
  ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();

-- 2. New join table for extended roles
CREATE TABLE public.project_organizations (
  id              INT8 GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project_id      INT8 NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
  organization_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('client', 'performer', 'referrer', 'invoice_to', 'partner')),
  UNIQUE (project_id, organization_id, role)
);

-- 3. Add organization_id to tasks and projects
ALTER TABLE public.tasks 
  ADD COLUMN organization_id UUID REFERENCES public.organizations(id);

ALTER TABLE public.projects  
  ADD COLUMN organization_id UUID REFERENCES public.organizations(id),
  ADD COLUMN is_org_proxy BOOLEAN DEFAULT false,
  ADD COLUMN migrated_to_organization_id UUID REFERENCES public.organizations(id);

-- 4. Dedicated signal table for Quick Process fallbacks
CREATE TABLE public.project_creation_signals (
  id              INT8 GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project_name    TEXT NOT NULL,
  source          TEXT NOT NULL,
  raw_dump_id     INT8 REFERENCES public.raw_dumps(id),
  task_id         INT8 REFERENCES public.tasks(id),
  status          TEXT DEFAULT 'pending',
  created_at      TIMESTAMPTZ DEFAULT now(),
  resolved_at     TIMESTAMPTZ
);

CREATE INDEX idx_signals_pending ON public.project_creation_signals(status) WHERE status = 'pending';
