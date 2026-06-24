-- ==============================================================================
-- PHASE 2: Data Backfill (Idempotent, Additive, No Deletes)
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- BLOCK 1: Pre-validation
-- ------------------------------------------------------------------------------
-- Note: Execute these manually or via script to verify before state
-- SELECT COUNT(*) AS orgs_before FROM organizations;
-- SELECT COUNT(*) AS projects_org_null_before FROM projects WHERE organization_id IS NULL;
-- SELECT COUNT(*) AS tasks_org_null_before FROM tasks WHERE organization_id IS NULL;

-- ------------------------------------------------------------------------------
-- BLOCK 2: Seed & Rename Organizations
-- ------------------------------------------------------------------------------
BEGIN;

-- 2a. Rename Chennai North Fellowship
UPDATE organizations 
SET name = 'Ashraya Chennai North' 
WHERE name = 'Chennai North Fellowship' AND name != 'Ashraya Chennai North';

-- 2b. Insert missing organizations idempotently
INSERT INTO organizations (id, name, org_type, description, is_active)
VALUES 
  (gen_random_uuid(), 'Ashraya', 'sub_org', 'Ashraya top level', true),
  (gen_random_uuid(), 'PERSONAL', 'domain', 'Personal domain', true),
  (gen_random_uuid(), 'Smudge', 'client', 'Design agency', true)
ON CONFLICT (name) DO NOTHING;

COMMIT;

-- ------------------------------------------------------------------------------
-- BLOCK 3: Set Parent Hierarchy for Orgs
-- ------------------------------------------------------------------------------
BEGIN;

WITH org_mapping AS (
  SELECT 'Solvstrat' AS child, 'Crayon Biz' AS parent, 'company' AS type UNION ALL
  SELECT 'Qhord', 'Crayon Biz', 'company' UNION ALL
  SELECT 'Shield Identity', 'Solvstrat', 'client' UNION ALL
  SELECT 'Smudge', 'Solvstrat', 'client' UNION ALL
  SELECT 'Armour Cyber', 'Shield Identity', 'client' UNION ALL
  SELECT 'Equisoft', 'Armour Cyber', 'client' UNION ALL
  SELECT 'Amico', 'Armour Cyber', 'client' UNION ALL
  SELECT 'Ashraya Chennai', 'Ashraya', 'sub_org' UNION ALL
  SELECT 'Ashraya Chennai North', 'Ashraya Chennai', 'sub_org' UNION ALL
  SELECT 'Ashraya Chennai Central', 'Ashraya Chennai', 'sub_org' UNION ALL
  SELECT 'Ashraya India', 'Ashraya Chennai', 'sub_org' UNION ALL
  SELECT 'Crayon Biz', NULL, 'holding' UNION ALL
  SELECT 'Ashraya', NULL, 'sub_org' UNION ALL
  SELECT 'PERSONAL', NULL, 'domain'
)
UPDATE organizations o
SET 
  parent_organization_id = p.id,
  org_type = m.type
FROM org_mapping m
LEFT JOIN organizations p ON p.name = m.parent
WHERE o.name = m.child
  AND (o.parent_organization_id IS DISTINCT FROM p.id OR o.org_type IS DISTINCT FROM m.type);

COMMIT;

-- ------------------------------------------------------------------------------
-- BLOCK 4: Backfill surviving projects -> organization_id
-- ------------------------------------------------------------------------------
BEGIN;

WITH project_org_map AS (
  -- GRB Website (id=24) -> Solvstrat
  SELECT 24 AS project_id, 'Solvstrat' AS org_name UNION ALL
  -- Trust account rekyc (id=28) -> Ashraya
  SELECT 28, 'Ashraya' UNION ALL
  -- Ashraya Compliance (id=35) -> Ashraya
  SELECT 35, 'Ashraya' UNION ALL
  -- Bank Rekyc (id=36) -> Ashraya
  SELECT 36, 'Ashraya' UNION ALL
  -- Family & Home (id=5) -> PERSONAL
  SELECT 5, 'PERSONAL' UNION ALL
  -- Personal (id=32) -> PERSONAL
  SELECT 32, 'PERSONAL' UNION ALL
  -- Qhord Fund Transfers (id=33) -> Qhord
  SELECT 33, 'Qhord'
)
UPDATE projects p
SET organization_id = o.id
FROM project_org_map m
JOIN organizations o ON o.name = m.org_name
WHERE p.id = m.project_id
  AND p.organization_id IS NULL;

COMMIT;

-- ------------------------------------------------------------------------------
-- BLOCK 5: Backfill tasks -> organization_id (for tasks linked to org-proxy projects)
-- ------------------------------------------------------------------------------
BEGIN;

WITH task_org_map AS (
  -- Tasks mapped to Solvstrat project (id=2) -> Solvstrat org
  SELECT 2 AS old_project_id, 'Solvstrat' AS org_name UNION ALL
  -- Ashraya (id=17)
  SELECT 17, 'Ashraya' UNION ALL
  -- Armour Cyber (id=25)
  SELECT 25, 'Armour Cyber' UNION ALL
  -- Equisoft (id=27)
  SELECT 27, 'Equisoft' UNION ALL
  -- Qhord (id=20)
  SELECT 20, 'Qhord' UNION ALL
  -- Shield Identity (id=19)
  SELECT 19, 'Shield Identity' UNION ALL
  -- Crayon Biz (id=31)
  SELECT 31, 'Crayon Biz' UNION ALL
  -- Cashflow (id=3) -> Solvstrat (user decision)
  SELECT 3, 'Solvstrat' UNION ALL
  -- Gan Website (id=26) -> Solvstrat (user decision)
  SELECT 26, 'Solvstrat' UNION ALL
  -- Johan Project (id=21) -> Solvstrat (user decision)
  SELECT 21, 'Solvstrat'
)
UPDATE tasks t
SET organization_id = o.id
FROM task_org_map m
JOIN organizations o ON o.name = m.org_name
WHERE t.project_id = m.old_project_id
  AND t.organization_id IS NULL;

COMMIT;

-- ------------------------------------------------------------------------------
-- BLOCK 6: Insert project_organizations (roles)
-- ------------------------------------------------------------------------------
BEGIN;

WITH proj_roles AS (
  -- GRB Website (id=24): performer=Solvstrat, client=Smudge
  SELECT 24 AS project_id, 'Solvstrat' AS org_name, 'performer' AS role UNION ALL
  SELECT 24, 'Smudge', 'client' UNION ALL
  -- Trust account rekyc (id=28): performer=Ashraya
  SELECT 28, 'Ashraya', 'performer' UNION ALL
  -- Ashraya Compliance (id=35)
  SELECT 35, 'Ashraya', 'performer' UNION ALL
  -- Bank Rekyc (id=36)
  SELECT 36, 'Ashraya', 'performer' UNION ALL
  -- Qhord Fund Transfers (id=33)
  SELECT 33, 'Qhord', 'performer' UNION ALL
  -- Family & Home (id=5)
  SELECT 5, 'PERSONAL', 'performer' UNION ALL
  -- Personal (id=32)
  SELECT 32, 'PERSONAL', 'performer'
)
INSERT INTO project_organizations (project_id, organization_id, role)
SELECT r.project_id, o.id, r.role
FROM proj_roles r
JOIN organizations o ON o.name = r.org_name
ON CONFLICT (project_id, organization_id, role) DO NOTHING;

COMMIT;

-- ------------------------------------------------------------------------------
-- BLOCK 7: Mark legacy org-as-project rows as proxy/deprecated
-- ------------------------------------------------------------------------------
BEGIN;

WITH proxy_map AS (
  SELECT 2 AS project_id, 'Solvstrat' AS org_name UNION ALL
  SELECT 31, 'Crayon Biz' UNION ALL
  SELECT 20, 'Qhord' UNION ALL
  SELECT 17, 'Ashraya' UNION ALL
  SELECT 25, 'Armour Cyber' UNION ALL
  SELECT 27, 'Equisoft' UNION ALL
  SELECT 19, 'Shield Identity'
)
UPDATE projects p
SET 
  is_org_proxy = true,
  migrated_to_organization_id = o.id
FROM proxy_map m
JOIN organizations o ON o.name = m.org_name
WHERE p.id = m.project_id
  AND p.is_org_proxy = false;

COMMIT;

-- ------------------------------------------------------------------------------
-- BLOCK 8: Post-validation
-- ------------------------------------------------------------------------------
-- Note: Execute these to verify after state
-- SELECT COUNT(*) AS orgs_after FROM organizations;
-- SELECT COUNT(*) AS projects_org_null_after FROM projects WHERE organization_id IS NULL AND is_org_proxy = false;
-- SELECT COUNT(*) AS tasks_org_null_after FROM tasks WHERE organization_id IS NULL AND project_id IS NULL;
-- SELECT COUNT(*) AS project_orgs_inserted FROM project_organizations;
-- SELECT COUNT(*) AS proxies_marked FROM projects WHERE is_org_proxy = true;
