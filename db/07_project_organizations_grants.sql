-- db/07_project_organizations_grants.sql
-- Ad-hoc permission fix applied during Phase 5 verification
-- The project_organizations table was missing standard role grants, causing 42501 Permission Denied on inserts via service_role
-- Updated to strictly adhere to least-privilege: only service_role gets access since frontend/backend both use it.

-- 1. Revoke any wide-open grants that might have been accidentally added
REVOKE ALL ON TABLE public.project_organizations FROM public;
REVOKE ALL ON TABLE public.project_organizations FROM anon;
REVOKE ALL ON TABLE public.project_organizations FROM authenticated;

-- 2. Grant explicitly to service_role (used by backend and Next.js server components)
GRANT ALL ON TABLE public.project_organizations TO service_role;

-- 3. Enable RLS to ensure default-deny for web clients
ALTER TABLE public.project_organizations ENABLE ROW LEVEL SECURITY;

-- 4. Do the same for project_creation_signals to keep it internal
REVOKE ALL ON TABLE public.project_creation_signals FROM public;
REVOKE ALL ON TABLE public.project_creation_signals FROM anon;
REVOKE ALL ON TABLE public.project_creation_signals FROM authenticated;
GRANT ALL ON TABLE public.project_creation_signals TO service_role;
ALTER TABLE public.project_creation_signals ENABLE ROW LEVEL SECURITY;
