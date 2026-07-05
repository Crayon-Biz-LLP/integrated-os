-- Task Routing Correction Trigger
-- Part of Tier 5 Meta-Cognitive Learning Layer
--
-- Captures reassignment corrections when a task's project_id or organization_id
-- changes (e.g. Danny moves a task from project A to project B).
-- Each change is recorded as a telemetry observation so the pattern learner
-- can extract routing preferences.

CREATE OR REPLACE FUNCTION log_task_routing_change()
RETURNS TRIGGER AS $$
DECLARE
  keywords_json jsonb;
BEGIN
  -- Only fire on is_current rows that have actually changed project/org
  IF NEW.is_current = true
     AND (NEW.project_id IS DISTINCT FROM OLD.project_id
          OR NEW.organization_id IS DISTINCT FROM OLD.organization_id) THEN

    -- Build title keywords array (words > 3 chars, max 5)
    SELECT COALESCE(jsonb_agg(word), '[]'::jsonb) INTO keywords_json
    FROM (
      SELECT word
      FROM regexp_split_to_table(lower(COALESCE(NEW.title, '')), E'\\s+') AS word
      WHERE length(word) > 3
      LIMIT 5
    ) sub;

    -- Fail-open: telemetry insertion must never crash the task update
    BEGIN
      INSERT INTO public.subsystem_telemetry (
        subsystem,
        event_type,
        features,
        predicted,
        actual,
        outcome,
        source
      ) VALUES (
        'task_routing',
        'correction',
        jsonb_build_object(
          'title_keywords', keywords_json,
          'old_project_id', OLD.project_id,
          'new_project_id', NEW.project_id,
          'old_organization_id', OLD.organization_id,
          'new_organization_id', NEW.organization_id,
          'has_project_change', NEW.project_id IS DISTINCT FROM OLD.project_id,
          'has_org_change', NEW.organization_id IS DISTINCT FROM OLD.organization_id
        ),
        jsonb_build_object(
          'project_id', OLD.project_id,
          'organization_id', OLD.organization_id
        ),
        jsonb_build_object(
          'project_id', NEW.project_id,
          'organization_id', NEW.organization_id
        ),
        'corrected',
        'webhook'
      );
    EXCEPTION WHEN OTHERS THEN
      -- Swallow: telemetry failure must never crash the task update
      NULL;
    END;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;


CREATE TRIGGER trg_task_routing_change
AFTER UPDATE ON public.tasks
FOR EACH ROW
WHEN (pg_trigger_depth() = 0)
EXECUTE FUNCTION log_task_routing_change();
