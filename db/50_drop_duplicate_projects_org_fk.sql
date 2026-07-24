-- Migration 50: Drop duplicate FK on projects.organization_id
-- projects_organization_id_fkey (Supabase auto-generated, NO ACTION) duplicates
-- fk_projects_organization (our explicit, ON DELETE SET NULL DEFERRABLE).
-- The duplicate causes PostgREST PGRST201 on any embed query like
-- projects?select=...,organizations(name) — PostgREST can't disambiguate.
-- This crashed planner.py (PROJECT_UPDATE/TASK/NOTE/COMPLETION) and silently
-- omitted projects from all QUERY context.

ALTER TABLE projects DROP CONSTRAINT projects_organization_id_fkey;
