-- db/21_memory_clusters_grants.sql
-- Grant service_role permissions on memory clustering tables.
-- These tables were created without standard role grants, causing
-- 42501 Permission Denied on inserts via service_role.
-- Follows the same pattern as db/07_project_organizations_grants.sql.

-- 1. memory_clusters
REVOKE ALL ON TABLE public.memory_clusters FROM public;
REVOKE ALL ON TABLE public.memory_clusters FROM anon;
REVOKE ALL ON TABLE public.memory_clusters FROM authenticated;
GRANT ALL ON TABLE public.memory_clusters TO service_role;
ALTER TABLE public.memory_clusters ENABLE ROW LEVEL SECURITY;

-- 2. memory_cluster_members
REVOKE ALL ON TABLE public.memory_cluster_members FROM public;
REVOKE ALL ON TABLE public.memory_cluster_members FROM anon;
REVOKE ALL ON TABLE public.memory_cluster_members FROM authenticated;
GRANT ALL ON TABLE public.memory_cluster_members TO service_role;
ALTER TABLE public.memory_cluster_members ENABLE ROW LEVEL SECURITY;

-- 3. memory_cluster_runs
REVOKE ALL ON TABLE public.memory_cluster_runs FROM public;
REVOKE ALL ON TABLE public.memory_cluster_runs FROM anon;
REVOKE ALL ON TABLE public.memory_cluster_runs FROM authenticated;
GRANT ALL ON TABLE public.memory_cluster_runs TO service_role;
ALTER TABLE public.memory_cluster_runs ENABLE ROW LEVEL SECURITY;

-- 4. Also grant sequence permissions for SERIAL/BIGSERIAL columns
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO service_role;
