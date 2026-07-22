-- Migration 46: Add FK constraint on projects.organization_id
-- Hardens the org-linking fix from Part 63 (af6ba41).
-- 
-- Ensures every projects.organization_id value matches an existing
-- organizations row. Without this, future code paths could set
-- organization_id to a dangling UUID without any DB-level error.

-- Pre-check: verify no invalid org_ids exist before adding constraint
DO $$
DECLARE
    invalid_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO invalid_count
    FROM projects
    WHERE organization_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = projects.organization_id);

    IF invalid_count > 0 THEN
        RAISE EXCEPTION 'Cannot add FK constraint: % projects have organization_id pointing to nonexistent orgs. Run the backfill script first (scripts/backfill_project_orgs.py).', invalid_count;
    END IF;
END $$;

ALTER TABLE projects
  ADD CONSTRAINT fk_projects_organization
  FOREIGN KEY (organization_id)
  REFERENCES organizations(id)
  ON DELETE SET NULL
  DEFERRABLE INITIALLY DEFERRED;

COMMENT ON CONSTRAINT fk_projects_organization ON projects IS
  'Hardens org-linking: every project.organization_id must reference a valid organizations row. ON DELETE SET NULL means deleting an org nulls the link rather than deleting the project. DEFERRED allows batch operations within a transaction.';

-- Also add index for efficient filtering by org
CREATE INDEX IF NOT EXISTS idx_projects_organization_id
  ON projects (organization_id)
  WHERE organization_id IS NOT NULL;
