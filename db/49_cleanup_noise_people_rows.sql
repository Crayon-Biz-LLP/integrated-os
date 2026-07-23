-- Cleanup: Soft-delete active people rows for noise/generic labels.
--
-- These entries (Uncle, User, The Boys, etc.) are created by the entity
-- detector's Pattern B (capitalized words near context words) but are not
-- real people. Even when deleted from the Live tab, the people row sometimes
-- stays active because graph_node_id wasn't properly backfilled.
--
-- Once marked as deleted, resolve_canonical_label() will skip them and they
-- won't be recreated as graph_nodes.

UPDATE people SET
    deleted_at = NOW(),
    is_current = false,
    strategic_weight = 0,
    graph_node_id = NULL
WHERE LOWER(TRIM(name)) IN (
    'uncle',
    'the devil',
    'the boys',
    'jasmine',
    'user',
    'aunt',
    'father',
    'friends',
    'inbox',
    'acs',
    'anubis',
    'lof',
    'puppy rescue group',
    'igor anatolyevich',
    'meera',
    'paul washer',
    'cathy',
    'female leader'
)
AND (deleted_at IS NULL OR is_current = true);

-- Also check organizations table for same noise labels
UPDATE organizations SET
    is_active = false
WHERE LOWER(TRIM(name)) IN (
    'uncle', 'user', 'the devil', 'the boys', 'weekend rest',
    'aunt', 'father', 'friends', 'inbox'
)
AND is_active = true;

-- Verify
SELECT name, deleted_at, is_current
FROM people
WHERE LOWER(TRIM(name)) IN (
    'uncle', 'the devil', 'the boys', 'jasmine', 'user',
    'aunt', 'father', 'friends', 'inbox', 'acs', 'anubis',
    'lof', 'puppy rescue group', 'igor anatolyevich',
    'meera', 'paul washer', 'cathy', 'female leader'
)
ORDER BY name;
