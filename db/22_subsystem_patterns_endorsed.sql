-- Tier 5 Meta-Cognitive Learning Layer: Endorsement columns
-- Adds two counters to subsystem_patterns that track user approval signals
-- without conflating them with empirical correctness counts.
--
-- operator_endorsed_count:  Operator explicitly confirmed pattern correctness
--                           (future use — not wired to confidence).
-- soft_accepted_count:       Suggest-mode approval count — user said "stop asking, go ahead"
--                           (used for exclusion, not confidence).

ALTER TABLE subsystem_patterns
    ADD COLUMN IF NOT EXISTS operator_endorsed_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS soft_accepted_count     INTEGER DEFAULT 0;

COMMENT ON COLUMN subsystem_patterns.operator_endorsed_count IS
    'Operator explicit endorsement count. Separate from correct_count. Does NOT affect confidence.';

COMMENT ON COLUMN subsystem_patterns.soft_accepted_count IS
    'Suggest-mode approval count. User confirmed "stop asking" for this pattern. Does NOT affect confidence.';
