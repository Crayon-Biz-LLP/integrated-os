-- db/24_subsystem_patterns_grants.sql
-- Fixes permission issue (42501) on subsystem_patterns and subsystem_telemetry tables.
-- The original migration (21_subsystem_telemetry.sql) created both tables but never
-- granted access to service_role, causing runtime failures in emit_observation().
--
-- Also adds first_seen column for temporal diversity tracking (compression penalty).

-- =====================================================
-- PART 1: Grant permissions to service_role
-- =====================================================

-- subsystem_telemetry
REVOKE ALL ON TABLE public.subsystem_telemetry FROM public;
REVOKE ALL ON TABLE public.subsystem_telemetry FROM anon;
REVOKE ALL ON TABLE public.subsystem_telemetry FROM authenticated;
GRANT ALL ON TABLE public.subsystem_telemetry TO service_role;
ALTER TABLE public.subsystem_telemetry ENABLE ROW LEVEL SECURITY;

-- subsystem_patterns
REVOKE ALL ON TABLE public.subsystem_patterns FROM public;
REVOKE ALL ON TABLE public.subsystem_patterns FROM anon;
REVOKE ALL ON TABLE public.subsystem_patterns FROM authenticated;
GRANT ALL ON TABLE public.subsystem_patterns TO service_role;
ALTER TABLE public.subsystem_patterns ENABLE ROW LEVEL SECURITY;

-- =====================================================
-- PART 2: Add first_seen column for temporal diversity
-- =====================================================

ALTER TABLE public.subsystem_patterns
    ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ;

COMMENT ON COLUMN public.subsystem_patterns.first_seen IS
    'Timestamp of the first observation for this pattern. Used to compute time-span compression penalties.';
