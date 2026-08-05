-- Migration 77: Drop orphan DB functions that reference the removed mirror tables
-- (people/organizations dropped in migration 75).
--
-- temporal_people_update()       — trigger func; its trigger was dropped with the people table
-- enrich_person_from_edges()     — UPDATE people; no callers in codebase
-- Both break only if invoked; they are dead code referencing dropped tables.

DROP FUNCTION IF EXISTS public.temporal_people_update();
DROP FUNCTION IF EXISTS public.enrich_person_from_edges(bigint);
