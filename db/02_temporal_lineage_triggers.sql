-- Temporal Lineage Triggers for tasks and canonical_pages

-- Trigger Function for Tasks
CREATE OR REPLACE FUNCTION temporal_task_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
BEGIN
  IF NEW.is_current = true THEN
    -- Only version if something material changed
    IF NEW.title IS DISTINCT FROM OLD.title OR NEW.status IS DISTINCT FROM OLD.status OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.priority IS DISTINCT FROM OLD.priority OR NEW.deadline IS DISTINCT FROM OLD.deadline OR NEW.reminder_at IS DISTINCT FROM OLD.reminder_at THEN
      
      -- Insert the OLD state as a historical record
      INSERT INTO public.tasks (title, status, priority, project_id, estimated_minutes, is_revenue_critical, deadline, created_at, completed_at, google_task_id, reminder_at, google_event_id, duration_mins, source, email_id, dedup_key, updated_at, is_current, version, supersedes_id, recurrence)
      VALUES (OLD.title, OLD.status, OLD.priority, OLD.project_id, OLD.estimated_minutes, OLD.is_revenue_critical, OLD.deadline, OLD.created_at, OLD.completed_at, OLD.google_task_id, OLD.reminder_at, OLD.google_event_id, OLD.duration_mins, OLD.source, OLD.email_id, OLD.dedup_key, OLD.updated_at, false, OLD.version, OLD.supersedes_id, OLD.recurrence)
      RETURNING id INTO archived_id;
      
      -- Update the NEW row to increment version and point to the historical record
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_temporal_task_update
BEFORE UPDATE ON public.tasks
FOR EACH ROW
WHEN (pg_trigger_depth() = 0)
EXECUTE FUNCTION temporal_task_update();

-- Trigger Function for Canonical Pages
CREATE OR REPLACE FUNCTION temporal_canonical_pages_update()
RETURNS TRIGGER AS $$
DECLARE
  archived_id int8;
BEGIN
  IF NEW.is_current = true THEN
    -- Only version if content or title changed
    IF NEW.title IS DISTINCT FROM OLD.title OR NEW.content IS DISTINCT FROM OLD.content THEN
      
      -- Insert the OLD state as a historical record
      INSERT INTO public.canonical_pages (title, content, category, entity_id, embedding, updated_at, project_id, source_count, last_synth_at, is_sparse, is_archived, archived_at, archive_reason, is_current, version, supersedes_id)
      VALUES (OLD.title, OLD.content, OLD.category, OLD.entity_id, OLD.embedding, OLD.updated_at, OLD.project_id, OLD.source_count, OLD.last_synth_at, OLD.is_sparse, OLD.is_archived, OLD.archived_at, OLD.archive_reason, false, OLD.version, OLD.supersedes_id)
      RETURNING id INTO archived_id;
      
      -- Update the NEW row to increment version and point to the historical record
      NEW.version = OLD.version + 1;
      NEW.supersedes_id = archived_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_temporal_canonical_pages_update
BEFORE UPDATE ON public.canonical_pages
FOR EACH ROW
WHEN (pg_trigger_depth() = 0)
EXECUTE FUNCTION temporal_canonical_pages_update();
